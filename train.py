import os
import warnings

import torch
import torch.optim as optim
import torchvision.utils
from accelerate import Accelerator, DistributedDataParallelKwargs
from torchsampler import ImbalancedDatasetSampler
from pytorch_msssim import SSIM
from torch.utils.data import DataLoader
from torchmetrics.functional import peak_signal_noise_ratio, structural_similarity_index_measure
from tqdm import tqdm
from torchvision.utils import save_image

from config import Config
from data import get_training_data, get_test_data
from loss import Perceptual
from models import *
from utils import seed_everything, save_checkpoint

warnings.filterwarnings('ignore')

opt = Config('config.yml')

seed_everything(opt.OPTIM.SEED)

if not os.path.exists(opt.TRAINING.SAVE_DIR):
    os.makedirs(opt.TRAINING.SAVE_DIR)


def train():
    # Accelerate
    kwargs = [DistributedDataParallelKwargs(find_unused_parameters=True)]
    accelerator = Accelerator(kwargs_handlers=kwargs)
    device = accelerator.device
    config = {
        "dataset": opt.TRAINING.TRAIN_DIR,
        "model": opt.MODEL.SESSION
    }
    accelerator.init_trackers("uw", config=config)
    criterion_psnr = torch.nn.MSELoss()
    criterion_ssim = SSIM(data_range=1, size_average=True, channel=3).to(device)

    # Data Loader
    train_dir = opt.TRAINING.TRAIN_DIR
    val_dir = opt.TRAINING.VAL_DIR

    train_dataset = get_training_data(train_dir, opt.MODEL.FILM, {'w': opt.TRAINING.PS_W, 'h': opt.TRAINING.PS_H})
    trainloader = DataLoader(dataset=train_dataset, batch_size=opt.OPTIM.BATCH_SIZE, shuffle=True, num_workers=16,
                             drop_last=False, pin_memory=True, sampler=ImbalancedDatasetSampler(train_dataset))
    val_dataset = get_test_data(val_dir, opt.MODEL.FILM, {'w': opt.TESTING.PS_W, 'h': opt.TESTING.PS_H})
    testloader = DataLoader(dataset=val_dataset, batch_size=1, shuffle=False, num_workers=8, drop_last=False,
                            pin_memory=True)

    model = UWEnhancer()

    optimizer = optim.AdamW(model.parameters(), lr=opt.OPTIM.LR_INITIAL,
                            betas=(0.9, 0.999), eps=1e-8)
    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, opt.OPTIM.NUM_EPOCHS, eta_min=opt.OPTIM.LR_MIN)

    trainloader, testloader = accelerator.prepare(trainloader, testloader)
    model = accelerator.prepare(model)
    optimizer, scheduler = accelerator.prepare(optimizer, scheduler)

    start_epoch = 1
    best_psnr = 0

    # training
    for epoch in range(start_epoch, opt.OPTIM.NUM_EPOCHS + 1):
        model.train()
        train_loss = 0

        for i, data in enumerate(tqdm(trainloader)):
            # get the inputs; data is a list of [target, input, filename]
            tar = data[0].contiguous()
            inp = data[1].contiguous()

            # forward
            optimizer.zero_grad()
            res = model(inp).contiguous()

            loss_psnr = criterion_psnr(res, tar)
            loss_ssim = 1 - criterion_ssim(res, tar)
            loss_perceptual = Perceptual()

            train_loss = loss_perceptual(res, tar) + loss_psnr + 0.4 * loss_ssim

            # backward
            accelerator.backward(train_loss)
            optimizer.step()

        scheduler.step()
        print("epoch: {}, Loss: {}".format(epoch, train_loss))

        # testing
        if epoch % opt.TRAINING.VAL_AFTER_EVERY == 0:
            model.eval()
            with torch.no_grad():
                psnr = 0
                ssim = 0
                for idx, test_data in enumerate(tqdm(testloader)):
                    # get the inputs; data is a list of [targets, inputs, filename]
                    tar = test_data[0]
                    inp = test_data[1].contiguous()

                    # print("res_shape: ", res.shape)
                    # print("tar_shape: ", tar.shape)

                    res = model(inp).contiguous()
                    if res.shape != tar.shape:
                        res = torch.nn.functional.interpolate(res, (tar.shape[2], tar.shape[3]))
                    # save_image(res, os.path.join(os.getcwd(), "result", str(idx) + '_pred.png'))
                    # res, tar = accelerator.gather((res, tar))

                    psnr += peak_signal_noise_ratio(res, tar, data_range=1)
                    ssim += structural_similarity_index_measure(res, tar, data_range=1)

                psnr /= len(testloader)
                ssim /= len(testloader)

                if psnr > best_psnr:
                    # save model
                    best_psnr = psnr
                    save_checkpoint({
                        'epoch': epoch,
                        'state_dict': model.state_dict(),
                        'optimizer': optimizer.state_dict(),
                    }, epoch, opt.TRAINING.SAVE_DIR)

                accelerator.log({
                    "PSNR": psnr,
                    "SSIM": ssim
                }, step=epoch)

                print(
                    "epoch: {}, PSNR: {}, SSIM: {}, best PSNR: {}".format(epoch, psnr, ssim, best_psnr))

    accelerator.end_training()


if __name__ == '__main__':
    train()
