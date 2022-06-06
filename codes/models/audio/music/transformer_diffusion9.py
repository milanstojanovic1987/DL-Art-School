import itertools

import torch
import torch.nn as nn
import torch.nn.functional as F

from models.audio.music.music_quantizer2 import MusicQuantizer2
from models.diffusion.nn import timestep_embedding, normalization, zero_module, conv_nd, linear
from models.diffusion.unet_diffusion import TimestepBlock, AttentionBlock, TimestepEmbedSequential
from models.lucidrains.x_transformers import Encoder
from trainer.networks import register_model
from utils.util import checkpoint, print_network


def is_latent(t):
    return t.dtype == torch.float

def is_sequence(t):
    return t.dtype == torch.long


class MultiGroupEmbedding(nn.Module):
    def __init__(self, tokens, groups, dim):
        super().__init__()
        self.m = nn.ModuleList([nn.Embedding(tokens, dim // groups) for _ in range(groups)])

    def forward(self, x):
        h = [embedding(x[:, :, i]) for i, embedding in enumerate(self.m)]
        return torch.cat(h, dim=-1)


class ResBlock(TimestepBlock):
    def __init__(
        self,
        channels,
        emb_channels,
        dropout,
        out_channels=None,
        dims=2,
        kernel_size=3,
        efficient_config=False,
        use_scale_shift_norm=False,
    ):
        super().__init__()
        self.channels = channels
        self.emb_channels = emb_channels
        self.dropout = dropout
        self.out_channels = out_channels or channels
        self.use_scale_shift_norm = use_scale_shift_norm
        padding = {1: 0, 3: 1, 5: 2}[kernel_size]
        eff_kernel = 1 if efficient_config else 3
        eff_padding = 0 if efficient_config else 1

        self.in_layers = nn.Sequential(
            normalization(channels),
            nn.SiLU(),
            conv_nd(dims, channels, self.out_channels, eff_kernel, padding=eff_padding),
        )

        self.emb_layers = nn.Sequential(
            nn.SiLU(),
            linear(
                emb_channels,
                2 * self.out_channels if use_scale_shift_norm else self.out_channels,
            ),
        )
        self.out_layers = nn.Sequential(
            normalization(self.out_channels),
            nn.SiLU(),
            nn.Dropout(p=dropout),
            zero_module(
                conv_nd(dims, self.out_channels, self.out_channels, kernel_size, padding=padding)
            ),
        )

        if self.out_channels == channels:
            self.skip_connection = nn.Identity()
        else:
            self.skip_connection = conv_nd(dims, channels, self.out_channels, eff_kernel, padding=eff_padding)

    def forward(self, x, emb):
        """
        Apply the block to a Tensor, conditioned on a timestep embedding.

        :param x: an [N x C x ...] Tensor of features.
        :param emb: an [N x emb_channels] Tensor of timestep embeddings.
        :return: an [N x C x ...] Tensor of outputs.
        """
        return checkpoint(
            self._forward, x, emb
        )

    def _forward(self, x, emb):
        h = self.in_layers(x)
        emb_out = self.emb_layers(emb).type(h.dtype)
        while len(emb_out.shape) < len(h.shape):
            emb_out = emb_out[..., None]
        if self.use_scale_shift_norm:
            out_norm, out_rest = self.out_layers[0], self.out_layers[1:]
            scale, shift = torch.chunk(emb_out, 2, dim=1)
            h = out_norm(h) * (1 + scale) + shift
            h = out_rest(h)
        else:
            h = h + emb_out
            h = self.out_layers(h)
        return self.skip_connection(x) + h


class DiffusionLayer(TimestepBlock):
    def __init__(self, model_channels, dropout, num_heads):
        super().__init__()
        self.resblk = ResBlock(model_channels, model_channels, dropout, model_channels, dims=1, use_scale_shift_norm=True)
        self.attn = AttentionBlock(model_channels, num_heads, relative_pos_embeddings=True)

    def forward(self, x, time_emb):
        y = self.resblk(x, time_emb)
        return self.attn(y)
    

class TransformerDiffusion(nn.Module):
    """
    A diffusion model composed entirely of stacks of transformer layers. Why would you do it any other way?
    """
    def __init__(
            self,
            model_channels=512,
            prenet_layers=3,
            num_layers=8,
            in_channels=256,
            input_vec_dim=512,
            out_channels=512,  # mean and variance
            dropout=0,
            use_fp16=False,
            # Parameters for regularization.
            unconditioned_percentage=.1,  # This implements a mechanism similar to what is used in classifier-free training.
    ):
        super().__init__()

        self.in_channels = in_channels
        self.model_channels = model_channels
        self.out_channels = out_channels
        self.dropout = dropout
        self.unconditioned_percentage = unconditioned_percentage
        self.enable_fp16 = use_fp16

        self.inp_block = conv_nd(1, in_channels, model_channels, 3, 1, 1)

        self.time_embed = nn.Sequential(
            linear(model_channels, model_channels),
            nn.SiLU(),
            linear(model_channels, model_channels),
        )

        self.input_converter = nn.Linear(input_vec_dim, model_channels)
        self.code_converter = Encoder(
                    dim=model_channels,
                    depth=prenet_layers,
                    heads=model_channels//64,
                    ff_dropout=dropout,
                    attn_dropout=dropout,
                    use_rmsnorm=True,
                    ff_glu=True,
                    rotary_pos_emb=True,
                    zero_init_branch_output=True,
                    ff_mult=1,
                )

        self.unconditioned_embedding = nn.Parameter(torch.randn(1,1,model_channels))
        self.intg = nn.Conv1d(model_channels*2, model_channels, kernel_size=1)
        self.layers = TimestepEmbedSequential(*[DiffusionLayer(model_channels, dropout, model_channels // 64) for _ in range(num_layers)])

        self.out = nn.Sequential(
            normalization(model_channels),
            nn.SiLU(),
            zero_module(conv_nd(1, model_channels, out_channels, 3, padding=1)),
        )

        self.debug_codes = {}

    def get_grad_norm_parameter_groups(self):
        groups = {
            'layers': list(self.layers.parameters()) + list(self.inp_block.parameters()),
            'code_converters': list(self.input_converter.parameters()) + list(self.code_converter.parameters()),
            'time_embed': list(self.time_embed.parameters()),
        }
        return groups

    def timestep_independent(self, codes, expected_seq_len):
        code_emb = self.input_converter(codes)

        # Mask out the conditioning branch for whole batch elements, implementing something similar to classifier-free guidance.
        if self.training and self.unconditioned_percentage > 0:
            unconditioned_batches = torch.rand((code_emb.shape[0], 1, 1),
                                               device=code_emb.device) < self.unconditioned_percentage
            code_emb = torch.where(unconditioned_batches, self.unconditioned_embedding.repeat(codes.shape[0], 1, 1),
                                   code_emb)
        code_emb = self.code_converter(code_emb)

        expanded_code_emb = F.interpolate(code_emb.permute(0,2,1), size=expected_seq_len, mode='nearest').permute(0,2,1)
        return expanded_code_emb

    def forward(self, x, timesteps, codes=None, conditioning_input=None, precomputed_code_embeddings=None, conditioning_free=False):
        if precomputed_code_embeddings is not None:
            assert codes is None and conditioning_input is None, "Do not provide precomputed embeddings and the other parameters. It is unclear what you want me to do here."

        unused_params = []
        if conditioning_free:
            code_emb = self.unconditioned_embedding.repeat(x.shape[0], x.shape[-1], 1)
            unused_params.extend(list(self.code_converter.parameters()))
        else:
            if precomputed_code_embeddings is not None:
                code_emb = precomputed_code_embeddings
            else:
                code_emb = self.timestep_independent(codes, x.shape[-1])
            unused_params.append(self.unconditioned_embedding)
        code_emb = code_emb.permute(0,2,1)

        blk_emb = self.time_embed(timestep_embedding(timesteps, self.model_channels))
        x = self.inp_block(x)
        x = self.intg(torch.cat([x, code_emb], dim=1))
        for layer in self.layers:
            x = checkpoint(layer, x, blk_emb)

        x = x.float()
        out = self.out(x)

        # Involve probabilistic or possibly unused parameters in loss so we don't get DDP errors.
        extraneous_addition = 0
        for p in unused_params:
            extraneous_addition = extraneous_addition + p.mean()
        out = out + extraneous_addition * 0

        return out


class TransformerDiffusionWithQuantizer(nn.Module):
    def __init__(self, freeze_quantizer_until=20000, **kwargs):
        super().__init__()

        self.internal_step = 0
        self.freeze_quantizer_until = freeze_quantizer_until
        self.diff = TransformerDiffusion(**kwargs)
        self.quantizer = MusicQuantizer2(inp_channels=256, inner_dim=[1024], codevector_dim=1024, codebook_size=256,
                                        codebook_groups=2, max_gumbel_temperature=4, min_gumbel_temperature=.5)
        self.quantizer.quantizer.temperature = self.quantizer.min_gumbel_temperature
        del self.quantizer.up

    def update_for_step(self, step, *args):
        self.internal_step = step
        qstep = max(0, self.internal_step - self.freeze_quantizer_until)
        self.quantizer.quantizer.temperature = max(
            self.quantizer.max_gumbel_temperature * self.quantizer.gumbel_temperature_decay ** qstep,
                    self.quantizer.min_gumbel_temperature,
                )

    def forward(self, x, timesteps, truth_mel, conditioning_input, disable_diversity=False, conditioning_free=False):
        quant_grad_enabled = self.internal_step > self.freeze_quantizer_until
        with torch.set_grad_enabled(quant_grad_enabled):
            proj, diversity_loss = self.quantizer(truth_mel, return_decoder_latent=True)
            proj = proj.permute(0,2,1)

        # Make sure this does not cause issues in DDP by explicitly using the parameters for nothing.
        if not quant_grad_enabled:
            unused = 0
            for p in self.quantizer.parameters():
                unused = unused + p.mean() * 0
            proj = proj + unused
            diversity_loss = diversity_loss * 0

        diff = self.diff(x, timesteps, codes=proj, conditioning_input=conditioning_input, conditioning_free=conditioning_free)
        if disable_diversity:
            return diff
        return diff, diversity_loss

    def get_debug_values(self, step, __):
        if self.quantizer.total_codes > 0:
            return {'histogram_codes': self.quantizer.codes[:self.quantizer.total_codes]}
        else:
            return {}

    def get_grad_norm_parameter_groups(self):
        groups = {
            'attention_layers': list(itertools.chain.from_iterable([lyr.attn.parameters() for lyr in self.diff.layers])),
            'res_layers': list(itertools.chain.from_iterable([lyr.resblk.parameters() for lyr in self.diff.layers])),
            'quantizer_encoder': list(self.quantizer.encoder.parameters()),
            'quant_codebook': [self.quantizer.quantizer.codevectors],
            'out': list(self.diff.out.parameters()),
            'x_proj': list(self.diff.inp_block.parameters()),
            'layers': list(self.diff.layers.parameters()),
            'code_converters': list(self.diff.input_converter.parameters()) + list(self.diff.code_converter.parameters()),
            'time_embed': list(self.diff.time_embed.parameters()),
        }
        return groups


@register_model
def register_transformer_diffusion9(opt_net, opt):
    return TransformerDiffusion(**opt_net['kwargs'])


@register_model
def register_transformer_diffusion8_with_quantizer(opt_net, opt):
    return TransformerDiffusionWithQuantizer(**opt_net['kwargs'])


"""
# For TFD5
if __name__ == '__main__':
    clip = torch.randn(2, 256, 400)
    aligned_sequence = torch.randn(2,100,512)
    cond = torch.randn(2, 256, 400)
    ts = torch.LongTensor([600, 600])
    model = TransformerDiffusion(model_channels=3072, model_channels=1536, model_channels=1536)
    torch.save(model, 'sample.pth')
    print_network(model)
    o = model(clip, ts, aligned_sequence, cond)
"""

if __name__ == '__main__':
    clip = torch.randn(2, 256, 400)
    cond = torch.randn(2, 256, 400)
    ts = torch.LongTensor([600, 600])
    model = TransformerDiffusionWithQuantizer(model_channels=1024, input_vec_dim=1024, num_layers=16, prenet_layers=6)
    model.get_grad_norm_parameter_groups()

    quant_weights = torch.load('D:\\dlas\\experiments\\train_music_quant_r4\\models\\5000_generator.pth')
    #diff_weights = torch.load('X:\\dlas\\experiments\\train_music_diffusion_tfd5\\models\\48000_generator_ema.pth')
    model.quantizer.load_state_dict(quant_weights, strict=False)
    #model.diff.load_state_dict(diff_weights)

    torch.save(model.state_dict(), 'sample.pth')
    print_network(model)
    o = model(clip, ts, clip, cond)

