# Copyright (c) 2021, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from nemo.collections.nlp.parts.nlp_overrides import NLPCheckpointConnector
from random import shuffle

import torch
from megatron import fused_kernels, mpu
from megatron.data.data_samplers import build_pretraining_data_loader
from megatron.data.gpt_dataset import build_train_valid_test_datasets
from megatron.global_vars import get_args, get_tokenizer
from megatron.model import GPTModel
from megatron.utils import average_losses_across_data_parallel_group, get_ltor_masks_and_position_ids
from omegaconf.dictconfig import DictConfig
from pytorch_lightning.trainer.trainer import Trainer

from nemo.collections.nlp.models.nlp_model import NLPModel
from nemo.collections.nlp.modules.common.megatron.megatron_init import initialize_megatron_for_nemo
from nemo.utils import AppState, logging


class MegatronGPTModel(NLPModel):
    """
    Megatron GPT pretraining
    """

    def __init__(self, cfg: DictConfig, trainer: Trainer):
        super().__init__(cfg, trainer=trainer)
        app_state = AppState()
        app_state.global_rank = trainer.global_rank
        app_state.world_size = trainer.world_size
        app_state.model_parallel_size = cfg.get('tensor_model_parallel_size', 1)
        app_state.model_parallel_rank = compute_model_parallel_rank(
            app_state.global_rank, app_state.model_parallel_size
        )

        initialize_megatron_for_nemo(
            world_size=app_state.world_size,
            global_rank=app_state.global_rank,
            micro_batch_size=cfg.get('micro_batch_size', 1),
            tensor_model_parallel_size=cfg.get('tensor_model_parallel_size', 1),
            tensor_model_parallel_rank=app_state.model_parallel_rank,
            encoder_seq_length=cfg.get('encoder_seq_length', 512),
            num_layers=cfg.get('num_layers', 1),
            hidden_size=cfg.get('hidden_size', 16),
            num_attention_heads=cfg.get('num_attention_heads', 1),
            max_position_embeddings=cfg.get('max_position_embeddings', 512),
            tokenizer_type='GPT2BPETokenizer',
            vocab_file=cfg.vocab_file,
            merge_file=cfg.merge_file,
        )
        args = get_args()

        fused_kernels.load(args)

        self.model = GPTModel(
            num_tokentypes=0, parallel_output=True, pre_process=cfg.pre_process, post_process=cfg.post_process
        )

    def forward(self, tokens, position_ids, attention_mask, labels):
        output_tensor = self.model(tokens, position_ids, attention_mask, labels=labels)
        return output_tensor

    def training_step(self, batch, batch_idx):
        tokens, labels, loss_mask, attention_mask, position_ids = self.process_batch(batch)
        output_tensor = self(tokens, position_ids, attention_mask, labels)
        loss = self.loss_func(loss_mask, output_tensor)
        self.log('train_loss', loss)
        # Reduced loss for logging.
        averaged_loss = average_losses_across_data_parallel_group([loss])
        self.log('reduced_train_loss', averaged_loss[0], prog_bar=True)
        return loss

    def validation_step(self, batch, batch_idx):
        tokens, labels, loss_mask, attention_mask, position_ids = self.process_batch(batch)
        output_tensor = self(tokens, position_ids, attention_mask, labels)
        loss = self.loss_func(loss_mask, output_tensor)
        # take the first k tokens and then generate text - compare with ground truth (won't look similar in general)
        """
        k = num_context
        n = max_generate_length
        context_tokens = tokens[0:k]
        while k < n:
            output_tensor = self(context_tokens)
            next_token = sample(output_tensor)
            context_tokens.append(next_token)
            k += 1
        """
        return loss

    def validation_epoch_end(self, outputs):
        averaged_loss = average_losses_across_data_parallel_group(outputs)
        self.log('val_loss', averaged_loss[0], prog_bar=True)

    def loss_func(self, loss_mask, output_tensor):
        losses = output_tensor.float()
        loss_mask = loss_mask.view(-1).float()
        loss = torch.sum(losses.view(-1) * loss_mask) / loss_mask.sum()  # sequence level nll
        return loss

    def process_batch(self, batch):
        args = get_args()
        tokenizer = get_tokenizer()

        # Items and their type.
        keys = ['text']
        datatype = torch.int64

        data = batch
        data_b = mpu.broadcast_data(keys, data, datatype)

        # Unpack.
        tokens_ = data_b['text'].long()
        labels = tokens_[:, 1:].contiguous()
        tokens = tokens_[:, :-1].contiguous()

        if self.cfg.debug:
            logging.info('debugging')
            tokens_list = tokens.detach().tolist()[0]
            labels_list = labels.detach().tolist()[0]
            logging.info(f'detokenize tokens: {tokenizer.detokenize(tokens_list)}')
            logging.info(f'detokenize labels: {tokenizer.detokenize(labels_list)}')

        # Get the masks and postition ids.
        attention_mask, loss_mask, position_ids = get_ltor_masks_and_position_ids(
            tokens, tokenizer.eod, args.reset_position_ids, args.reset_attention_mask, args.eod_mask_loss
        )

        return tokens, labels, loss_mask, attention_mask, position_ids

    def setup(self, stage=None):
        app_state = AppState()
        self._trainer.checkpoint_connector = NLPCheckpointConnector(self._trainer)

        logging.info('Building GPT datasets.')
        self._train_ds, self._validation_ds, self._test_ds = build_train_valid_test_datasets(
            data_prefix=self.cfg.train_ds.data_prefix,
            data_impl=self.cfg.train_ds.data_impl,
            splits_string=self.cfg.train_ds.splits_string,
            train_valid_test_num_samples=self.cfg.train_ds.train_valid_test_num_samples,
            seq_length=self.cfg.train_ds.seq_length,
            seed=self.cfg.train_ds.seed,
            skip_warmup=self.cfg.train_ds.skip_warmup,
        )
        self.setup_training_data(self.cfg.train_ds)
        self.setup_validation_data(self.cfg.train_ds)
        logging.info(f'Finished building GPT datasets.')

    def setup_training_data(self, cfg):
        # TODO: Add megatron specific sampler
        # see build_pretraining_data_loader from megatron-lm
        # consumed_samples = self.trainer.global_step
        if hasattr(self, '_train_ds'):
            self._train_dl = torch.utils.data.DataLoader(
                self._train_ds, num_workers=cfg.num_workers, pin_memory=True, batch_size=self.cfg.micro_batch_size,
            )

    def setup_validation_data(self, cfg):
        if hasattr(self, '_validation_ds'):
            self._validation_dl = torch.utils.data.DataLoader(
                self._validation_ds,
                num_workers=cfg.num_workers,
                pin_memory=True,
                shuffle=False,
                batch_size=self.cfg.micro_batch_size,
            )

    def list_available_models():
        pass
