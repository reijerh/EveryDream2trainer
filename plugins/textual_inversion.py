import json
import logging
import os.path

import torch
from colorama import Fore
import re

from plugins.plugins import BasePlugin
from train import EveryDreamTrainingState
from utils.sample_generator import clean_filename

""" 
This plugin adds custom tokens to the tokenizer and trains just these tokens, with the rest of the text encoder
disabled/frozen.

token/initialization config is in textual_inversion.json, same folder as this .py file.

For pure Textual Inversion training:
  "disable_textenc_training": false,
  "disable_unet_training": true
(Or you could unet training on too if you want, I didn't test this.)
  

In optimizer.json, the following "text_encoder_freezing" section is *required*:
    "text_encoder_freezing": {
        "unfreeze_last_n_layers": 0,
        "freeze_embeddings": false,
        "freeze_final_layer_norm": true,
        "freeze_position_embeddings": true
    }
In addition, you'll need a very high LR on the TE - maybe even as high as 1e-3. I recommend using the LR finder method.

"""

class TextualInversionPlugin(BasePlugin):

    def __init__(self):
        path = os.path.join(os.path.dirname(__file__), "textual_inversion.json")
        logging.info(f" * Textual Inversion plugin instantiated, loading config from {path}")
        with open(path, 'rt') as f:
            self.config = json.load(f)

        self.training_tokens = None
        self.training_token_ids = None
        self.padding_tokens = {}
        self.padding_token_ids = {}
        self.textual_inversion_tokens_only_grads = None

    def on_model_load(self, **kwargs):
        ed_state: EveryDreamTrainingState = kwargs.get('ed_state')
        def get_token_ids(t: str):
            return ed_state.tokenizer.convert_tokens_to_ids(ed_state.tokenizer.tokenize(t))

        # check for correctly configured text encoder training
        disable_unet_training: bool = kwargs.get('disable_unet_training')
        disable_textenc_training: bool = kwargs.get('disable_textenc_training')
        #if not disable_unet_training or disable_textenc_training:
        #    logging.error(f" * {Fore.LIGHTRED_EX}Textual Inversion plugin REQUIRES {Fore.RESET}\"disable_unet_training\": true{Fore.LIGHTRED_EX} and {Fore.RESET}\"disable_textenc_training\": false{Fore.LIGHTRED_EX} in your train.json{Fore.RESET}")
        #    raise RuntimeError("Unet training must be disabled and text encoder training enabled")
        num_te_layers = len(ed_state.text_encoder.text_model.encoder.layers)
        optimizer_config: dict = kwargs.get('optimizer_config')
        if (optimizer_config is None or
            'text_encoder_freezing' not in optimizer_config or
            optimizer_config['text_encoder_freezing'].get('freeze_embeddings') != False or
            optimizer_config['text_encoder_freezing'].get('freeze_final_layer_norm') != True or
            optimizer_config['text_encoder_freezing'].get('unfreeze_last_n_layers', num_te_layers) > 0
        ):
            required_js_fragment = {"text_encoder_freezing": {"freeze_embeddings": False, "unfreeze_last_n_layers": 0, "freeze_final_layer_norm": True}}
            logging.error(f" * {Fore.LIGHTRED_EX}Textual Inversion plugin REQUIRES the following json fragment in your optimizer config:{Fore.RESET}")
            logging.error(f" * {Fore.LIGHTRED_EX}  {json.dumps(required_js_fragment)}{Fore.RESET}")
            raise RuntimeError("Misconfigured optimizer config")


        training_tokens = set()
        for token_info in self.config['tokens']:
            start_token = token_info['token']
            vector_length = token_info.get('vector_length', 1)
            print(f" * Textual Inversion training on '{start_token}' with vector length {vector_length}")
            this_padding_tokens = [f"{start_token}_pad!!!_{n+1}" for n in range(vector_length-1)]
            self.padding_tokens[start_token] = this_padding_tokens
            training_tokens.update([start_token] + this_padding_tokens)

        tokens_to_add = [t for t in training_tokens if len(get_token_ids(t))>1]
        logging.info(
            f" * Textual inversion training adding the following tokens: {sorted(tokens_to_add)}")
        tokens_to_overwrite = [t for t in training_tokens if t not in tokens_to_add]
        if any(tokens_to_overwrite):
            logging.warning(f" * {Fore.LIGHTYELLOW_EX}Textual inversion training overwriting the following tokens: {tokens_to_overwrite}{Fore.RESET}")

        num_added_tokens = ed_state.tokenizer.add_tokens(tokens_to_add)
        if num_added_tokens != len(tokens_to_add):
            raise RuntimeError(f"Tokens not added successfully - tried to add {len(tokens_to_add)} but only added {num_added_tokens}")
        ed_state.text_encoder.resize_token_embeddings(len(ed_state.tokenizer))

        added_token_ids = []
        for token in tokens_to_add:
            token_ids = get_token_ids(token)
            if len(token_ids) != 1:
                raise RuntimeError(f"Tokens not added succesfully - expected 1 token id for {token}, found {len(token_ids)}")
            token_id = token_ids[0]
            added_token_ids.append(token_id)

        for trigger_token, padding_tokens in self.padding_tokens.items():
            this_padding_token_ids = [get_token_ids(t)[0] for t in padding_tokens]
            self.padding_token_ids[trigger_token] = this_padding_token_ids

        # copy initializer embedding
        input_embeddings = ed_state.text_encoder.get_input_embeddings()
        for token_info in self.config['tokens']:
            vector_length = token_info.get('vector_length', 1)
            # make sure it's very long
            initializer_text = " ".join([token_info['initializer']] * vector_length)
            with torch.no_grad():
                initializer_token_ids_full = ed_state.tokenizer(initializer_text,
                               truncation=True,
                               padding="max_length",
                               max_length=ed_state.tokenizer.model_max_length,
                               ).input_ids
                initializer_embedding_full = ed_state.text_encoder(
                    torch.tensor(initializer_token_ids_full, device=ed_state.text_encoder.device).unsqueeze(0), output_hidden_states=True
                ).last_hidden_state
            initializer_embedding = initializer_embedding_full[0][1:vector_length+1]

            trigger_token = token_info['token']
            trigger_and_padding_tokens = [trigger_token] + self.padding_tokens[trigger_token]
            for i in range(vector_length):
                token_ids = get_token_ids(trigger_and_padding_tokens[i])
                token_id = token_ids[0]
                # don't clobber trained embeddings when resuming
                if token_id in tokens_to_add:
                    input_embeddings.weight.data[token_id] = initializer_embedding[i]

        overwriting_token_ids = [get_token_ids(t)[0] for t in tokens_to_overwrite]
        self.training_tokens = tokens_to_add + tokens_to_overwrite
        self.training_token_ids = added_token_ids + overwriting_token_ids

        # get indices of non-training tokens (ie tokens whose grads should be reset to 0 every step)
        total_len = len(ed_state.text_encoder.get_input_embeddings().weight)
        all_token_ids = torch.arange(total_len, dtype=torch.int)

        untrained_tokens_working = torch.cat((all_token_ids, torch.tensor(self.training_token_ids, dtype=torch.int)))
        uniques, counts = untrained_tokens_working.unique(return_counts=True)
        untrained_tokens = uniques[counts == 1]
        self.non_training_token_ids = untrained_tokens

    def on_backpropagation(self, **kwargs):
        # Zero out the gradients for all token embeddings except the newly added
        # embeddings for the concept, as we only want to optimize the concept embeddings
        index_grads_to_zero = self.non_training_token_ids
        ed_state: EveryDreamTrainingState = kwargs['ed_state']
        grads = ed_state.text_encoder.get_input_embeddings().weight.grad
        #print(f"before zeroing: global sum {torch.sum(grads)}, training sum {torch.sum(grads[self.training_token_ids])}, individual: {grads[self.training_token_ids]}")
        grads.data[index_grads_to_zero, :] = grads.data[index_grads_to_zero, :].fill_(0)
        #print(f"after zeroing: global sum {torch.sum(grads)}, training sum {torch.sum(grads[self.training_token_ids])}, individual: {grads[self.training_token_ids]}")


    def on_model_save(self, **kwargs):
        ed_state: EveryDreamTrainingState = kwargs['ed_state']
        embeddings = ed_state.text_encoder.get_input_embeddings()
        save_folder = kwargs['diffusers_save_path']
        for token_id, token in zip(self.training_token_ids, self.training_tokens):
            if token not in self.padding_token_ids:
                continue
            padding_token_ids = self.padding_token_ids[token]
            all_token_ids = [token_id] + padding_token_ids
            full_embedding = embeddings.weight[all_token_ids]
            _save_embedding(token=token, embedding=full_embedding, save_folder=save_folder)

    def transform_caption(self, caption:str) -> str:
        return self.expand_trigger_tokens(caption)

    def modify_sample_prompt(self, prompt: str) -> str:
        return self.expand_trigger_tokens(prompt)

    def expand_trigger_tokens(self, caption: str) -> str:
        tokens = self.config['tokens']
        # for multi-vector tokens, replace the trigger token with a padded sequence of the correct length.
        # eg "hat*" with vector length 3 -> "hat* hat*_pad!!!_1 hat*_pad!!!_2"
        for t in tokens:
            trigger = t['token']
            replacement = " ".join([trigger] + self.padding_tokens[trigger])
            caption = re.sub(trigger, replacement, caption)
        return caption



def _save_embedding(token, embedding, save_folder):
    dict_to_save = {token: embedding}
    token_name_safe = clean_filename(token)
    ti_folder = os.path.join(save_folder, 'textual_inversions')
    os.makedirs(ti_folder, exist_ok=True)
    save_path = os.path.join(ti_folder, token_name_safe + '.bin')
    logging.info(f"Saving textual inversion for '{token}' to {save_path}")
    torch.save(dict_to_save, save_path)

