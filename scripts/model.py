"""EfficientNet-B0 기반 heatmap keypoint 검출기.

독립(independent) 모델과 통합(joint) 모델은 같은 클래스를 쓰고, 출력 점 개수만 다르다.
- backbone: ImageNet 사전학습 EfficientNet-B0 (흑백 영상을 3채널로 복제해 사전학습 가중치 그대로 사용)
- neck: stride 8/16/32 특징을 합쳐 64×64 특징맵 (stride 8)
- heatmap head: 점마다 heatmap 1장 → spatial softmax → soft-argmax(기댓값)로 연속 좌표
- existence head: 전역 특징으로 점별 존재 확률 (시야 밖 지표 처리)
"""
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision


class KeypointNet(nn.Module):
    def __init__(self, n_points, pretrained=True, ch=128):
        super().__init__()
        w = torchvision.models.EfficientNet_B0_Weights.IMAGENET1K_V1 if pretrained else None
        feats = torchvision.models.efficientnet_b0(weights=w).features
        self.stem = feats[:4]     # stride 8,  40 ch
        self.mid = feats[4:6]     # stride 16, 112 ch
        self.deep = feats[6:]     # stride 32, 1280 ch
        self.l8, self.l16, self.l32 = nn.Conv2d(40, ch, 1), nn.Conv2d(112, ch, 1), nn.Conv2d(1280, ch, 1)
        self.fuse = nn.Sequential(nn.Conv2d(ch, ch, 3, padding=1), nn.BatchNorm2d(ch), nn.ReLU(inplace=True),
                                  nn.Conv2d(ch, ch, 3, padding=1), nn.BatchNorm2d(ch), nn.ReLU(inplace=True))
        self.heatmap = nn.Conv2d(ch, n_points, 1)
        self.exist = nn.Sequential(nn.Linear(1280, 256), nn.ReLU(inplace=True), nn.Dropout(0.1), nn.Linear(256, n_points))
        self.n = n_points
        self.register_buffer('mean', torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1))
        self.register_buffer('std', torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1))

    def forward(self, x):
        """x: (B,1,512,512) in [0,1]. 반환 좌표는 입력 픽셀 좌표(0–512)."""
        x = (x.repeat(1, 3, 1, 1) - self.mean) / self.std
        f8 = self.stem(x)
        f16 = self.mid(f8)
        f32 = self.deep(f16)
        p = self.l32(f32)
        p = F.interpolate(p, size=f16.shape[-2:], mode='bilinear', align_corners=False) + self.l16(f16)
        p = F.interpolate(p, size=f8.shape[-2:], mode='bilinear', align_corners=False) + self.l8(f8)
        logits = self.heatmap(self.fuse(p))                       # (B,K,64,64)
        B, K, H, W = logits.shape
        prob = logits.flatten(2).float().softmax(-1).view(B, K, H, W)
        ys = (torch.arange(H, device=x.device, dtype=prob.dtype) + 0.5) * (x.shape[-2] / H)
        xs = (torch.arange(W, device=x.device, dtype=prob.dtype) + 0.5) * (x.shape[-1] / W)
        kx = (prob.sum(2) * xs).sum(-1)
        ky = (prob.sum(3) * ys).sum(-1)
        exist = self.exist(f32.mean((2, 3)))
        return torch.stack([kx, ky], -1), prob, exist

