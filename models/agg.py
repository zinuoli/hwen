import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.models as models
from torchvision.models import VGG16_Weights


def conv(X, W, s):
    x1_use = X[:, :, s, :, :]
    x1_out = torch.einsum('ncskj,dckj->nds', x1_use, W)
    return x1_out


class ConvLayer(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, stride, dilation=1, bias=True, groups=1, norm='in',
                 nonlinear='relu'):
        super(ConvLayer, self).__init__()
        reflection_padding = (kernel_size + (dilation - 1) * (kernel_size - 1)) // 2
        self.reflection_pad = nn.ReflectionPad2d(reflection_padding)
        self.conv2d = nn.Conv2d(in_channels, out_channels, kernel_size, stride, groups=groups, bias=bias,
                                dilation=dilation)
        self.norm = norm
        self.nonlinear = nonlinear

        if norm == 'bn':
            self.normalization = nn.BatchNorm2d(out_channels)
        elif norm == 'in':
            self.normalization = nn.InstanceNorm2d(out_channels, affine=False)
        else:
            self.normalization = None

        if nonlinear == 'relu':
            self.activation = nn.ReLU(inplace=True)
        elif nonlinear == 'leakyrelu':
            self.activation = nn.LeakyReLU(0.2)
        elif nonlinear == 'PReLU':
            self.activation = nn.PReLU()
        else:
            self.activation = None

    def forward(self, x):
        out = self.conv2d(self.reflection_pad(x))
        if self.normalization is not None:
            out = self.normalization(out)
        if self.activation is not None:
            out = self.activation(out)

        return out


class SelfAttention(nn.Module):
    def __init__(self, channels, k, nonlinear='relu'):
        super(SelfAttention, self).__init__()
        self.channels = channels
        self.k = k
        self.nonlinear = nonlinear

        self.linear1 = nn.Linear(channels, channels // k)
        self.linear2 = nn.Linear(channels // k, channels)
        self.global_pooling = nn.AdaptiveAvgPool2d((1, 1))

        if nonlinear == 'relu':
            self.activation = nn.ReLU(inplace=True)
        elif nonlinear == 'leakyrelu':
            self.activation = nn.LeakyReLU(0.2)
        elif nonlinear == 'PReLU':
            self.activation = nn.PReLU()
        else:
            raise ValueError

    def attention(self, x):
        N, C, H, W = x.size()
        out = torch.flatten(self.global_pooling(x), 1)
        out = self.activation(self.linear1(out))
        out = torch.sigmoid(self.linear2(out)).view(N, C, 1, 1)

        return out.mul(x)

    def forward(self, x):
        return self.attention(x)


class SPP(nn.Module):
    def __init__(self, in_channels, out_channels, num_layers=4, interpolation_type='bilinear'):
        super(SPP, self).__init__()
        self.conv = nn.ModuleList()
        self.num_layers = num_layers
        self.interpolation_type = interpolation_type

        for _ in range(self.num_layers):
            self.conv.append(
                ConvLayer(in_channels, in_channels, kernel_size=1, stride=1, dilation=1, nonlinear='leakyrelu',
                          norm=None))

        self.fusion = ConvLayer((in_channels * (self.num_layers + 1)), out_channels, kernel_size=3, stride=1,
                                norm='False', nonlinear='leakyrelu')

    def forward(self, x):

        N, C, H, W = x.size()
        out = []

        for level in range(self.num_layers):
            out.append(F.interpolate(self.conv[level](
                F.avg_pool2d(x, kernel_size=2 * 2 ** (level + 1), stride=2 * 2 ** (level + 1),
                             padding=2 * 2 ** (level + 1) % 2)), size=(H, W), mode=self.interpolation_type))

        out.append(x)

        return self.fusion(torch.cat(out, dim=1))


class Aggreation(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size=3):
        super(Aggreation, self).__init__()
        self.attention = SelfAttention(in_channels, k=8, nonlinear='relu')
        self.conv = ConvLayer(in_channels, out_channels, kernel_size=3, stride=1, dilation=1, nonlinear='leakyrelu',
                              norm=None)

    def forward(self, x):
        return self.conv(self.attention(x))


class Backbone(nn.Module):
    def __init__(self):
        super(Backbone, self).__init__()

        modules = (models.vgg16(weights=VGG16_Weights.DEFAULT).features[:-1])

        self.block1 = modules[0:4]
        self.block2 = modules[4:9]
        self.block3 = modules[9:16]
        self.block4 = modules[16:23]
        self.block5 = modules[23:]

        for param in self.parameters():
            param.requires_grad = False

    def forward(self, x):
        N, C, H, W = x.size()

        out = [x]

        out.append(self.block1(out[-1]))
        out.append(self.block2(out[-1]))
        out.append(self.block3(out[-1]))
        out.append(self.block4(out[-1]))
        out.append(self.block5(out[-1]))

        return torch.cat(
            [(F.interpolate(item, size=(H, W), mode='bicubic') if sum(item.size()[2:]) != sum(x.size()[2:]) else item)
             for item in out], dim=1).detach()


class Agg(nn.Module):
    def __init__(self, channels=64):
        super(Agg, self).__init__()

        self.backbone = Backbone()

        self.fusion = ConvLayer(in_channels=1475, out_channels=channels, kernel_size=1, stride=1, norm=None,
                                nonlinear='leakyrelu')

        # Stage0
        self.block0_1 = ConvLayer(in_channels=channels, out_channels=channels, kernel_size=1, stride=1, norm=None,
                                  nonlinear='leakyrelu')
        self.block0_2 = ConvLayer(in_channels=channels, out_channels=channels, kernel_size=3, stride=1, norm=None,
                                  nonlinear='leakyrelu')

        self.aggreation0_rgb = Aggreation(in_channels=channels * 2, out_channels=channels)

        # Stage1
        self.block1_1 = ConvLayer(in_channels=channels, out_channels=channels, kernel_size=3, stride=1, dilation=2,
                                  norm=None, nonlinear='leakyrelu')
        self.block1_2 = ConvLayer(in_channels=channels, out_channels=channels, kernel_size=3, stride=1, dilation=4,
                                  norm=None, nonlinear='leakyrelu')

        self.aggreation1_rgb = Aggreation(in_channels=channels * 3, out_channels=channels)

        # Stage2
        self.block2_1 = ConvLayer(in_channels=channels, out_channels=channels, kernel_size=3, stride=1, dilation=8,
                                  norm=None, nonlinear='leakyrelu')
        self.block2_2 = ConvLayer(in_channels=channels, out_channels=channels, kernel_size=3, stride=1, dilation=16,
                                  norm=None, nonlinear='leakyrelu')

        self.aggreation2_rgb = Aggreation(in_channels=channels * 3, out_channels=channels)

        # Stage3
        self.block3_1 = ConvLayer(in_channels=channels, out_channels=channels, kernel_size=3, stride=1, dilation=32,
                                  norm=None, nonlinear='leakyrelu')
        self.block3_2 = ConvLayer(in_channels=channels, out_channels=channels, kernel_size=3, stride=1, dilation=64,
                                  norm=None, nonlinear='leakyrelu')

        self.aggreation3_rgb = Aggreation(in_channels=channels * 4, out_channels=channels)

        # Stage4
        self.spp_img = SPP(in_channels=channels, out_channels=channels, num_layers=4, interpolation_type='bicubic')

        self.block4_1 = nn.Conv2d(in_channels=channels, out_channels=3, kernel_size=1, stride=1)

    def forward(self, x):
        out_backbone = self.backbone(x)

        out = self.fusion(out_backbone)

        # Stage0
        out0_1 = self.block0_1(out)
        out0_2 = self.block0_2(out0_1)

        agg0_rgb = self.aggreation0_rgb(torch.cat((out0_1, out0_2), dim=1))

        # Stage1
        out1_1 = self.block1_1(agg0_rgb)
        out1_2 = self.block1_2(out1_1)

        agg1_rgb = self.aggreation1_rgb(torch.cat((agg0_rgb, out1_1, out1_2), dim=1))

        # Stage2
        out2_1 = self.block2_1(agg1_rgb)
        out2_2 = self.block2_2(out2_1)

        agg2_rgb = self.aggreation2_rgb(torch.cat((agg1_rgb, out2_1, out2_2), dim=1))

        # Stage3
        out3_1 = self.block3_1(agg2_rgb)
        out3_2 = self.block3_2(out3_1)

        agg3_rgb = self.aggreation3_rgb(torch.cat((agg1_rgb, agg2_rgb, out3_1, out3_2), dim=1))

        # Stage4
        spp_rgb = self.spp_img(agg3_rgb)
        out_rgb = self.block4_1(spp_rgb)

        return out_rgb


if __name__ == '__main__':
    # 360 * 540
    # torch.Size([1, 3, 45, 68])
    # torch.Size([1, 3, 90, 136])
    # torch.Size([1, 3, 180, 270])
    # torch.Size([1, 3, 360, 540])
    # 480 * 640
    # torch.Size([1, 3, 60, 80])
    # torch.Size([1, 3, 120, 160])
    # torch.Size([1, 3, 240, 320])
    # torch.Size([1, 3, 480, 640])
    # 480 * 720
    # torch.Size([1, 3, 60, 90])
    # torch.Size([1, 3, 120, 180])
    # torch.Size([1, 3, 240, 360])
    # torch.Size([1, 3, 480, 720])
    # 512 * 512
    # torch.Size([1, 3, 64, 64])
    # torch.Size([1, 3, 128, 128])
    # torch.Size([1, 3, 256, 256])
    # torch.Size([1, 3, 512, 512])
    tensor = torch.randn(1, 3, 128, 128).cuda()
    model = Agg().cuda()
    res = model(tensor)
    print(res.shape)