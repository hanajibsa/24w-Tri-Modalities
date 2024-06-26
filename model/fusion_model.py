import numpy as np
import torch
from timm.models.layers import trunc_normal_
from torch import nn as nn
import torch.nn.functional as F

from model.utils.utils import normalize_embeddings
from model.utils.layers import get_projection
from model.utils.fusion_transformer import FusionTransformer
from model.utils.davenet import load_DAVEnet
from model.utils.projection import projection_net
from model.utils.classifier import Classifier
from model.utils.CommonEncoder import CommonEncoder

class EverythingAtOnceModel(nn.Module):
    def __init__(self,
                 args,
                 embed_dim=1024,
                 video_embed_dim=4096,
                 text_embed_dim=300,
                 video_max_tokens=None,
                 text_max_tokens=None,
                 audio_max_num_STFT_frames=None,
                 projection_dim=6144,
                 projection='gated',
                 strategy_audio_pooling='none',
                 davenet_v2=True,
                 individual_projections=True,
                 use_positional_emb=False
                 ):
        super().__init__()

        self.embed_dim = embed_dim
        self.use_softmax = args.use_softmax
        self.use_cls_token = args.use_cls_token
        self.num_classes = args.num_classes

        self.fusion = FusionTransformer(embed_dim=self.embed_dim, use_softmax=self.use_softmax, use_cls_token=self.use_cls_token, num_classes = self.num_classes)

        self.args = args
        self.token_projection = args.token_projection

        self.individual_projections = individual_projections
        self.use_positional_emb = use_positional_emb
        self.strategy_audio_pooling = strategy_audio_pooling

        self.video_norm_layer = nn.LayerNorm(self.embed_dim, eps=1e-6)
        self.text_norm_layer = nn.LayerNorm(self.embed_dim, eps=1e-6)
        self.audio_norm_layer = nn.LayerNorm(self.embed_dim, eps=1e-6)
        self.norm_layer = nn.LayerNorm(self.embed_dim, eps=1e-6)

        # audio token preprocess
        self.davenet = load_DAVEnet(v2=davenet_v2)

        if audio_max_num_STFT_frames is not None:
            if davenet_v2:
                audio_max_tokens = int(audio_max_num_STFT_frames / 64)
            else:
                audio_max_tokens = int(audio_max_num_STFT_frames / 16)
            self.audio_max_tokens = audio_max_tokens
        else:
            self.audio_max_tokens = None

        if self.use_positional_emb:
            assert video_max_tokens is not None
            assert text_max_tokens is not None
            assert audio_max_num_STFT_frames is not None
            self.video_pos_embed = nn.Parameter(torch.zeros(1, video_max_tokens, self.embed_dim))
            self.text_pos_embed = nn.Parameter(torch.zeros(1, text_max_tokens, self.embed_dim))
            self.audio_pos_embed = nn.Parameter(torch.zeros(1, self.audio_max_tokens, self.embed_dim))
        else:
            self.video_pos_embed = None
            self.text_pos_embed = None
            self.audio_pos_embed = None

        audio_embed_dim = 4096 if davenet_v2 else 1024
        if self.token_projection == 'projection_net':
            self.token_proj = projection_net(embed_dim=self.embed_dim)
        else:
            self.video_token_proj = get_projection(video_embed_dim, self.embed_dim, self.token_projection)
            self.text_token_proj = get_projection(text_embed_dim, self.embed_dim, self.token_projection)
            self.audio_token_proj = get_projection(audio_embed_dim, self.embed_dim, self.token_projection)
        

        self.init_weights()

        self.commonencoder = CommonEncoder(common_dim=self.embed_dim, latent_dim=512)
        self.classifier1 = Classifier(latent_dim=2048)
        self.classifier2 = Classifier(latent_dim=2048)
        self.classifier3 = Classifier(latent_dim=2048)
        self.commonencoder=CommonEncoder(common_dim=self.embed_dim, latent_dim=512)

    def init_weights(self):
        for weights in [self.video_pos_embed, self.audio_pos_embed, self.text_pos_embed]:
            if weights is not None:
                trunc_normal_(weights, std=.02)

    def _check_and_fix_if_input_empty(self, x, attention_mask):
        nonempty_input_mask = attention_mask.sum(-1) != 0

        # if all tokens of modality is empty, add one masking token
        empty_input_mask = nonempty_input_mask == 0
        n_masking_tokens = 1
        x[empty_input_mask, :n_masking_tokens] = self.fusion.masking_token.type(x.dtype)
        attention_mask[empty_input_mask, :n_masking_tokens] = 1
        return x, attention_mask, nonempty_input_mask

    def extract_video_tokens(self, video):
        x = self.video_token_proj(video)
        x = self.video_norm_layer(x)
        return x

      
    def extract_audio_tokens(self, audio, audio_STFT_nframes):
        audio = self.davenet(audio)
        audio = audio.permute(0, 2, 1)
        audio = self.audio_token_proj(audio)
        audio = self.audio_norm_layer(audio)

        # audio, attention_mask, nonempty_input_mask = self._check_and_fix_if_input_empty(audio, attention_mask)
        # special_token_mask = attention_mask == 0
        return audio
    
    def extract_text_tokens(self, text):
        x = self.text_token_proj(text)
        x = self.text_norm_layer(x)
        return x
    
    def extract_tokens(self, video, audio, text, nframes):
        audio, text, video = self.token_proj(video, audio, nframes, text)
        
        return audio, text, video

    def forward(self, video, audio, nframes, text, category, force_cross_modal=False):
        if self.token_projection == 'projection_net':
            audio_raw_embed, text_raw_embed, video_raw_embed = self.extract_tokens(video, audio, text, nframes)
            video_raw_embed = torch.unsqueeze(video_raw_embed, 1) # ([16, 1, 1024] [16, 1024, 1024] [16, 30, 1024]
        else:
            text_raw_embed = self.extract_text_tokens(text) # [16, 30, 4096]
            video_raw_embed = self.extract_video_tokens(video) # [16, 4096]
            audio_raw_embed = self.extract_audio_tokens(audio, nframes) # [16, 80, 4096]

            
        ### Visual - Audio
        va = self.fusion(key=video_raw_embed,
                            query=audio_raw_embed)
        av = self.fusion(key=audio_raw_embed, query=video_raw_embed)

        va = va.mean(dim=1)
        av = av.mean(dim=1)
        vav = torch.cat((va,av), dim=1).view(va.size(0),-1)
        vav= self.classifier1(vav)

        ##Audio - Text
        at = self.fusion(key=audio_raw_embed,
                            query=text_raw_embed)
        ta = self.fusion(key=text_raw_embed,
                            query=audio_raw_embed)

        at = at.mean(dim=1)
        ta = ta.mean(dim=1)
        ata = torch.cat((at,ta), dim=1).view(va.size(0),-1)
        ata= self.classifier2(ata)


        ##Text - Video
        tv = self.fusion(key=text_raw_embed,
                            query=video_raw_embed)
        #tv = tv + text_raw_embed
        vt = self.fusion(key=video_raw_embed,
                            query=text_raw_embed)

        tv = tv.mean(dim=1)
        vt = vt.mean(dim=1)
        tvt = torch.cat((tv,vt), dim=1).view(va.size(0),-1)
        tvt= self.classifier3(tvt)


        return vav, ata, tvt