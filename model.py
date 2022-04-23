from numpy import gradient
import torch
import torch.nn as nn
import torch.nn.functional as F
from utils import *

class Embedding:
    def __init__(self, input_dim, length, include_input = True, log_sampling = True):
        super(Embedding, self).__init__()
        self.input_dim = input_dim
        self.length = length
        self.functs = [torch.sin, torch.cos]
        self.output_dim = 2 * length * input_dim + include_input * self.input_dim
        if log_sampling:
            self.freq = torch.pow(2, torch.linspace(0, length - 1, steps = length) )
        else:
            self.freq = torch.linspace(1, 2**(length-1), steps = length)

    def embed(self, x):
        ##  x: [N_rays, 3]
        ## self.freq: [length]
        
        embed_vec = x[..., None] * self.freq # [N_rays, 3, length]
        embed_vec = torch.stack([func(embed_vec) for func in self.functs], dim = -1) # [N_rays, 3, length, 2]
        embed_vec = embed_vec.permute([0,2,3,1]).reshape([embed_vec.shape[0], -1])  # [N_rays, length, 2, 3] [N_rays, 3 * 2 * length]
        x = torch.cat([x, embed_vec], dim = -1)
        return x
    

class GeometryNetwork(nn.Module):
    def __init__(self, input_dim, embed_length, output_dim = 256, D = 8, W = 256, skip_connect = [4], r = 3, bound_scale = 1):
        super(GeometryNetwork, self).__init__()
        
        self.skip_connect = skip_connect
        self.input_dim = input_dim
        self.output_dim = output_dim
        self.embedding = Embedding(input_dim, embed_length)
        self.r = r
        self.bound_scale = bound_scale
        self.pts_linears = nn.ModuleList(
            
            [nn.Linear(self.embedding.output_dim, W)] + [nn.Linear(W,W-self.input_dims) if i+1 in self.skip_connect else nn.Linear(W,W) for i in range(1, D-1)]
        )

        self.feature_linear = nn.Linear(W, output_dim + 1)
        self.softplus = nn.Softplus(beta = 100)

    def output(self, x):
        
        x = self.embedding.embed(x)
        h = x
        for i, model in enumerate(self.pts_linears):
            h = self.softplus(model(h))
            if i in self.skip_connect:
                h = torch.cat([x,h], dim = -1)
        
        h = self.feature_linear(h)
        return h[..., :1], h[..., 1:]
                
    def gradient_for_loss(self, x):
        x.require_grad_(True)
        d, _ = self.output(x)
        gradient = get_gradient(x,d)
        return gradient

    def forward(self, x):
        x.require_grad_(True)
        d, feature = self.output(x)
        bound = self.bound_scale * (self.r - torch.norm(x,p=2,dim=-1))
        d = torch.minimum(d, bound)
        gradient = get_gradient(x, d)
        return d, feature, gradient

        
class RadienceFieldNetwork(nn.Module):
    def __init__(self, input_dim, embed_length, feature_length, D = 4, W = 256):
        super(RadienceFieldNetwork, self).__init__()
        self.output_dim = 3

        self.embedding = Embedding(input_dim, embed_length)

        self.input_dim = 3 + self.embedding.output_dim + 3 + feature_length
        
        self.pts_linears = nn.ModuleList(
            [nn.Linear(self.input_dim, W)]+[nn.Linear(W, W) for i in range(D-1)] + [nn.Linear(W, self.output_dim)]
        )

    def forward(self, points, view, normals, feature):
        view = self.embedding.embed(view)
        x = torch.cat([points, view, normals, feature])
        for i, model in enumerate(self.pts_linears):
            if i == 0:
                x = model(x)
            else:
                x = model(F.relu(x))
        
        return F.sigmoid(x, dim = -1)

class VolSDF(nn.Module):
    def __init__(self, r = 3, beta = 0.1, position_length=10, view_length=4, feature_dim = 256, bound_scale = 1):
        super(VolSDF, self).__init__()

        self.position_network = GeometryNetwork(3, position_length, output_dim=feature_dim, bound_scale = bound_scale)
        self.radience_field_network = RadienceFieldNetwork(3, view_length, feature_dim)

        self.beta = torch.Tensor([beta])
        self.r = r

    def forward(self, x, view):
        beta = torch.abs(beta) + 1e-7
        d, feature, gradient = self.position_network(x)
        raw_color = self.radience_field_network(x, view, gradient, feature)
        density = (0.5+0.5*torch.sign(d)*(1-torch.exp(-torch.abs(d)/beta))) * 1 / beta
        return density, raw_color

    def gradient(self, x):
        return self.position_network.gradient_for_loss(x)

    def get_sdf(self, x):
        d, _ = self.position_network.output(x)
        d = torch.minimum(d, self.r - torch.norm(x, dim = -1, p=2))
        return d

    def density_from_sdf(self, d): 
        beta = torch.abs(beta) + 1e-7
        density =(0.5+0.5*torch.sign(d)*(1-torch.exp(-torch.abs(d)/self.beta)) )* 1 / beta
        return density

    def density(self, x):
        d, _ = self.position_network.output(x)
        d = torch.minimum(d, self.r - torch.norm(x, dim = -1, p=2)) 
        density = self.density_from_sdf(d)
        return density