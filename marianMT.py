from transformers import MarianMTModel, MarianTokenizer
from transformers.models.marian.modeling_marian import MarianDecoder, MarianModel
from transformers.modeling_outputs import Seq2SeqLMOutput
from torch.nn import BCEWithLogitsLoss, CrossEntropyLoss, MSELoss, BCELoss, KLDivLoss, Sigmoid, LogSigmoid, LogSoftmax
from torch.nn.functional import softmax
from datasets import load_dataset
import torch
import copy

from neural_constr import init_sentiment, neural_constr_function

class ConstrainedMT(MarianMTModel):
    def __init__(self, config, rc_layers=3):
        super().__init__(config)
        self.rc_config = copy.deepcopy(config)
        self.rc_config.decoder_layers = rc_layers
        self.model_rc_decoder = MarianDecoder(self.rc_config, self.model.shared)
        self.model_rc_linear = torch.nn.Linear(self.rc_config.d_model, self.model.shared.num_embeddings)
        self.constraint_factor = 0.0
        self.temperature = 1.0
        self.regularization = 5.0
        self.reg_type = 3
        self.log_sigmoid_fct = LogSigmoid()
        self.log_softmax_fct = LogSoftmax(dim=-1)

    def set_constraint_factor(self, constraint_factor):
        self.constraint_factor = constraint_factor

    def set_temperature(self, temperature):
        self.temperature = temperature

    def set_regularization(self, regularization):
        self.regularization = regularization

    def set_reg_type(self, n):
        self.reg_type = n

    def zero_init_rc(self):
        torch.nn.init.zeros_(self.model_rc_linear.weight)
        torch.nn.init.zeros_(self.model_rc_linear.bias)

    def my_BCELoss(self, x, y, mask=None):
        result = -(y * torch.log(x) + (1 - y) * torch.log(1 - x))
        if mask == None:
            return result.mean()
        else:
            factor = 1.0 / mask.sum()
            return (result * mask * factor).sum()
        #return -(y * torch.log(x) + (1 - y) * torch.log(1 - x))

    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        decoder_input_ids=None,
        decoder_attention_mask=None,
        head_mask=None,
        decoder_head_mask=None,
        cross_attn_head_mask=None,
        encoder_outputs=None,
        past_key_values=None,
        inputs_embeds=None,
        decoder_inputs_embeds=None,
        use_cache=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
        rc_labels=None,
        rc_weights=None,
        fine_tune=False,
    ):
        # past_key_values is num_layers * (self_attn(k, v), cross_attn(k, v)) 
        #                               * [batch_size, num_heads, length - 1, d / num_heads]
        # our rc should be a seq2seq decoder model to fit this form

        if past_key_values is not None:
            mt_pkv, rc_pkv = past_key_values
        else:
            mt_pkv = None
            rc_pkv = None

        outputs = super().forward(
            input_ids=input_ids,
            attention_mask=attention_mask,
            decoder_input_ids=decoder_input_ids,
            decoder_attention_mask=decoder_attention_mask,
            head_mask=head_mask,
            decoder_head_mask=decoder_head_mask,
            cross_attn_head_mask=cross_attn_head_mask,
            encoder_outputs=encoder_outputs,
            past_key_values=mt_pkv,
            inputs_embeds=inputs_embeds,
            decoder_inputs_embeds=decoder_inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        rc_decoder_outputs = self.model_rc_decoder(
            input_ids=decoder_input_ids,
            attention_mask=decoder_attention_mask,
            encoder_hidden_states=outputs.encoder_last_hidden_state,
            encoder_attention_mask=attention_mask,
            head_mask=decoder_head_mask,
            cross_attn_head_mask=cross_attn_head_mask,
            past_key_values=rc_pkv,
            inputs_embeds=decoder_inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        mt_logits = self.log_softmax_fct(outputs.logits)
        constr_logits = self.model_rc_linear(rc_decoder_outputs[0])
        final_logits = mt_logits * self.temperature + self.log_sigmoid_fct(constr_logits) * self.constraint_factor

        loss1 = 0.0
        loss2 = 0.0

        length = decoder_input_ids.shape[1]

        loss = None

        if rc_labels is not None:

            for i in range(length - 1):
                pred_logits = torch.gather(constr_logits[:, i, :], 1, decoder_input_ids[:, i + 1].unsqueeze(1))

                if rc_weights is not None:
                    weights = softmax(rc_weights * (1.0 - self.temperature), dim=0)
                    loss_fct = BCEWithLogitsLoss(weight=
                        (decoder_attention_mask[:, i + 1] * weights).unsqueeze(1), reduction='sum')
                else:
                    loss_fct = BCEWithLogitsLoss(weight=decoder_attention_mask[:, i + 1].unsqueeze(1))

                cur_loss = loss_fct(pred_logits, rc_labels)
                loss1 += cur_loss

            loss = loss1

            if self.regularization > 0.0:
                for i in range(1, length):
                    pred_logits = torch.gather(constr_logits[:, i - 1, :], 1, decoder_input_ids[:, i].unsqueeze(1))
                    pred_probs = torch.sigmoid(pred_logits)
                    sum_log = torch.logsumexp(final_logits[:, i, :], dim=-1).unsqueeze(1)
                    sum_probs = torch.exp(sum_log)
                    sum_logits = torch.log(sum_probs / (1 - sum_probs))
                    ### wrong version
                    #loss_fct = BCEWithLogitsLoss(weight=decoder_attention_mask[:, i].unsqueeze(1))
                    #loss2 += self.regularization * loss_fct(pred_logits, sum_logits)

                    ### version 1 (p1-p2)^2
                    if self.reg_type == 1:
                        loss_fct = MSELoss(reduction='none')
                        if float(decoder_attention_mask[:, i].sum()) == 0:
                            continue
                        factor = self.regularization / float(decoder_attention_mask[:, i].sum())
                        loss2 += factor * (loss_fct(pred_probs, sum_probs) * decoder_attention_mask[:, i].unsqueeze(1)).sum()

                    ### version 2 (p1/p2 - 1)^2
                    elif self.reg_type == 2:
                        loss_fct = MSELoss(reduction='none')
                        if float(decoder_attention_mask[:, i].sum()) == 0:
                            continue
                        ref = torch.ones(pred_probs.shape).to(self.device)
                        factor = self.regularization / float(decoder_attention_mask[:, i].sum())
                        loss2 += factor * (loss_fct(sum_probs / pred_probs, ref) * decoder_attention_mask[:, i].unsqueeze(1)).sum()
                    
                    ### version 3 (KL(p1||p2))
                    elif self.reg_type == 3:
                        loss_fct = BCEWithLogitsLoss(weight=decoder_attention_mask[:, i].unsqueeze(1))
                        loss2 += self.regularization * (loss_fct(sum_logits, pred_probs) - loss_fct(pred_logits, pred_probs))
                                        
                #print (loss1.item(), loss2.item())

                loss = loss1 + loss2
        

        if fine_tune:
            loss_fct = CrossEntropyLoss(reduction='none')
            loss = loss_fct(mt_logits[:, :-1, :].reshape(-1, self.config.vocab_size), 
                decoder_input_ids[:, 1:].reshape(-1))
            weights = decoder_attention_mask[:, 1:].reshape(-1)
            loss = (loss * weights).mean()

        # self property about Rc
        # \sum_(y_i) p_\theta(y_i|x, y_<i) * Rc(y_<=i) = Rc(y_<i)

        
        return Seq2SeqLMOutput(
            loss=loss,
            logits=final_logits,
            past_key_values=(outputs.past_key_values, rc_decoder_outputs.past_key_values),
            decoder_hidden_states=outputs.decoder_hidden_states,
            decoder_attentions=outputs.decoder_attentions,
            cross_attentions=outputs.cross_attentions,
            encoder_last_hidden_state=outputs.encoder_last_hidden_state,
            encoder_hidden_states=outputs.encoder_hidden_states,
            encoder_attentions=outputs.encoder_attentions,
        )

    def save_rc(self, save_dir):
        decoder_dict = self.model_rc_decoder.state_dict()
        linear_dict = self.model_rc_linear.state_dict()
        print ("Saving Model to %s..."%(save_dir))
        torch.save((decoder_dict, linear_dict), save_dir)

    def save_all(self, save_dir):
        save_dict = self.state_dict()
        print ("Saving Model to %s..."%(save_dir))
        torch.save(save_dict, save_dir)


    @staticmethod
    def _reorder_cache(past, beam_idx):
        past1, past2 = past

        reordered_past1 = ()
        for layer_past in past1:
            # cached cross_attention states don't have to be reordered -> they are always the same
            reordered_past1 += (
                tuple(past_state.index_select(0, beam_idx) for past_state in layer_past[:2]) + layer_past[2:],
            )

        reordered_past2 = ()
        for layer_past in past2:
            # cached cross_attention states don't have to be reordered -> they are always the same
            reordered_past2 += (
                tuple(past_state.index_select(0, beam_idx) for past_state in layer_past[:2]) + layer_past[2:],
            )

        return (reordered_past1, reordered_past2)


class ConstrainedIndMT(MarianMTModel):
    def __init__(self, config, rc_layers=3):
        super().__init__(config)
        self.rc_config = copy.deepcopy(config)
        self.rc_config.decoder_layers = rc_layers
        self.rc_model = None
        self.rc_linear = torch.nn.Linear(config.d_model, config.vocab_size, bias=True)
        self.constraint_factor = 0.0
        self.temperature = 1.0
        self.regularization = 5.0
        self.reg_type = 3
        self.log_sigmoid_fct = LogSigmoid()
        self.log_softmax_fct = LogSoftmax(dim=-1)

    def rc_init(self, name):
        self.rc_model = MarianModel.from_pretrained(name)
        torch.nn.init.zeros_(self.rc_linear.weight)
        torch.nn.init.zeros_(self.rc_linear.bias)

    def set_constraint_factor(self, constraint_factor):
        self.constraint_factor = constraint_factor

    def set_temperature(self, temperature):
        self.temperature = temperature

    def set_regularization(self, regularization):
        self.regularization = regularization

    def set_reg_type(self, n):
        self.reg_type = n

    def zero_init_rc(self):
        torch.nn.init.zeros_(self.model_rc_linear.weight)
        torch.nn.init.zeros_(self.model_rc_linear.bias)

    def my_BCELoss(self, x, y, mask=None):
        result = -(y * torch.log(x) + (1 - y) * torch.log(1 - x))
        if mask == None:
            return result.mean()
        else:
            factor = 1.0 / mask.sum()
            return (result * mask * factor).sum()
        #return -(y * torch.log(x) + (1 - y) * torch.log(1 - x))

    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        decoder_input_ids=None,
        decoder_attention_mask=None,
        head_mask=None,
        decoder_head_mask=None,
        cross_attn_head_mask=None,
        encoder_outputs=None,
        past_key_values=None,
        inputs_embeds=None,
        decoder_inputs_embeds=None,
        use_cache=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
        rc_labels=None,
        rc_weights=None,
        fine_tune=False,
    ):
        # past_key_values is num_layers * (self_attn(k, v), cross_attn(k, v)) 
        #                               * [batch_size, num_heads, length - 1, d / num_heads]
        # our rc should be a seq2seq decoder model to fit this form

        if past_key_values is not None:
            mt_pkv, rc_pkv = past_key_values
        else:
            mt_pkv = None
            rc_pkv = None

        rc_outputs = self.rc_model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            decoder_input_ids=decoder_input_ids,
            decoder_attention_mask=decoder_attention_mask,
            head_mask=head_mask,
            decoder_head_mask=decoder_head_mask,
            cross_attn_head_mask=cross_attn_head_mask,
            encoder_outputs=encoder_outputs,
            past_key_values=rc_pkv,
            inputs_embeds=inputs_embeds,
            decoder_inputs_embeds=decoder_inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )
        constr_logits = self.rc_linear(rc_outputs[0])

        if rc_labels is None:

            outputs = super().forward(
                input_ids=input_ids,
                attention_mask=attention_mask,
                decoder_input_ids=decoder_input_ids,
                decoder_attention_mask=decoder_attention_mask,
                head_mask=head_mask,
                decoder_head_mask=decoder_head_mask,
                cross_attn_head_mask=cross_attn_head_mask,
                encoder_outputs=encoder_outputs,
                past_key_values=mt_pkv,
                inputs_embeds=inputs_embeds,
                decoder_inputs_embeds=decoder_inputs_embeds,
                use_cache=use_cache,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                return_dict=return_dict,
            )
            mt_logits = self.log_softmax_fct(outputs.logits)
            final_logits = mt_logits + self.log_sigmoid_fct(constr_logits) * self.constraint_factor

        else:
            outputs = rc_outputs

        
        loss1 = 0.0
        loss2 = 0.0

        length = decoder_input_ids.shape[1]

        loss = None

        if rc_labels is not None:

            for i in range(length - 1):
                pred_logits = torch.gather(constr_logits[:, i, :], 1, decoder_input_ids[:, i + 1].unsqueeze(1))

                if rc_weights is not None:
                    weights = softmax(rc_weights * (1.0 - self.temperature), dim=0)
                    loss_fct = BCEWithLogitsLoss(weight=
                        (decoder_attention_mask[:, i + 1] * weights).unsqueeze(1), reduction='sum')
                else:
                    loss_fct = BCEWithLogitsLoss(weight=decoder_attention_mask[:, i + 1].unsqueeze(1))

                cur_loss = loss_fct(pred_logits, rc_labels)
                loss1 += cur_loss

            final_logits = constr_logits
            loss = loss1

            if self.regularization > 0.0:
                final_logits = mt_logits * self.temperature + self.log_sigmoid_fct(constr_logits) * self.constraint_factor
                for i in range(1, length):
                    pred_logits = torch.gather(constr_logits[:, i - 1, :], 1, decoder_input_ids[:, i].unsqueeze(1))
                    pred_probs = torch.sigmoid(pred_logits)
                    sum_log = torch.logsumexp(final_logits[:, i, :], dim=-1).unsqueeze(1)
                    sum_probs = torch.exp(sum_log)
                    sum_logits = torch.log(sum_probs / (1 - sum_probs))
                    ### wrong version
                    #loss_fct = BCEWithLogitsLoss(weight=decoder_attention_mask[:, i].unsqueeze(1))
                    #loss2 += self.regularization * loss_fct(pred_logits, sum_logits)

                    ### version 1 (p1-p2)^2
                    if self.reg_type == 1:
                        loss_fct = MSELoss(reduction='none')
                        if float(decoder_attention_mask[:, i].sum()) == 0:
                            continue
                        factor = self.regularization / float(decoder_attention_mask[:, i].sum())
                        loss2 += factor * (loss_fct(pred_probs, sum_probs) * decoder_attention_mask[:, i].unsqueeze(1)).sum()

                    ### version 2 (p1/p2 - 1)^2
                    elif self.reg_type == 2:
                        loss_fct = MSELoss(reduction='none')
                        if float(decoder_attention_mask[:, i].sum()) == 0:
                            continue
                        ref = torch.ones(pred_probs.shape).to(self.device)
                        factor = self.regularization / float(decoder_attention_mask[:, i].sum())
                        loss2 += factor * (loss_fct(sum_probs / pred_probs, ref) * decoder_attention_mask[:, i].unsqueeze(1)).sum()
                    
                    ### version 3 (KL(p1||p2))
                    elif self.reg_type == 3:
                        loss_fct = BCEWithLogitsLoss(weight=decoder_attention_mask[:, i].unsqueeze(1))
                        loss2 += self.regularization * (loss_fct(sum_logits, pred_probs) - loss_fct(pred_logits, pred_probs))
                                        
                #print (loss1.item(), loss2.item())

                loss = loss1 + loss2
        

        if fine_tune:
            loss_fct = CrossEntropyLoss(reduction='none')
            loss = loss_fct(mt_logits[:, :-1, :].reshape(-1, self.config.vocab_size), 
                decoder_input_ids[:, 1:].reshape(-1))
            weights = decoder_attention_mask[:, 1:].reshape(-1)
            loss = (loss * weights).mean()

        # self property about Rc
        # \sum_(y_i) p_\theta(y_i|x, y_<i) * Rc(y_<=i) = Rc(y_<i)

        
        return Seq2SeqLMOutput(
            loss=loss,
            logits=final_logits,
            past_key_values=(outputs.past_key_values, rc_outputs.past_key_values),
            decoder_hidden_states=outputs.decoder_hidden_states,
            decoder_attentions=outputs.decoder_attentions,
            cross_attentions=outputs.cross_attentions,
            encoder_last_hidden_state=outputs.encoder_last_hidden_state,
            encoder_hidden_states=outputs.encoder_hidden_states,
            encoder_attentions=outputs.encoder_attentions,
        )

    def save_rc(self, save_dir):
        decoder_dict = self.rc_model.state_dict()
        linear_dict = self.rc_linear.state_dict()
        print ("Saving Model to %s..."%(save_dir))
        torch.save((decoder_dict, linear_dict), save_dir)

    def save_all(self, save_dir):
        save_dict = self.state_dict()
        print ("Saving Model to %s..."%(save_dir))
        torch.save(save_dict, save_dir)


    @staticmethod
    def _reorder_cache(past, beam_idx):
        past1, past2 = past

        reordered_past1 = ()
        for layer_past in past1:
            # cached cross_attention states don't have to be reordered -> they are always the same
            reordered_past1 += (
                tuple(past_state.index_select(0, beam_idx) for past_state in layer_past[:2]) + layer_past[2:],
            )

        reordered_past2 = ()
        for layer_past in past2:
            # cached cross_attention states don't have to be reordered -> they are always the same
            reordered_past2 += (
                tuple(past_state.index_select(0, beam_idx) for past_state in layer_past[:2]) + layer_past[2:],
            )

        return (reordered_past1, reordered_past2)

if __name__ == "__main__":
    src_text = [
        ">>esp<< Empirical method in natural language processing is better than association of computational linguistics, I mean EMNLP is better than ACL.",
        ">>esp<< Empirical method in natural language processing is better than association of computational linguistics, I mean EMNLP is better than ACL.",
        ">>esp<< Empirical method in natural language processing is better than association of computational linguistics, I mean EMNLP is better than ACL.",
        ">>esp<< Empirical method in natural language processing is better than association of computational linguistics, I mean EMNLP is better than ACL.",
        ">>esp<< Empirical method in natural language processing is better than association of computational linguistics, I mean EMNLP is better than ACL.",
        ">>esp<< Empirical method in natural language processing is better than association of computational linguistics, I mean EMNLP is better than ACL.",
    ]
    text_clean = [x.replace(">>esp<< ", "") for x in src_text]

    model_name = "Helsinki-NLP/opus-mt-en-es"
    tokenizer = MarianTokenizer.from_pretrained(model_name)
    #print(tokenizer.supported_language_codes)
    model = ConstrainedMT.from_pretrained(model_name)

    # constr_model, constr_tokenizer, _ = init_sentiment()
    # rc_labels, _ = neural_constr_function(constr_model, constr_tokenizer, text_clean)
    # print (rc_labels)

    encodings_dict = tokenizer(src_text, return_tensors="pt", padding=True, truncation=True, max_length=128)
    input_ids = torch.tensor(encodings_dict['input_ids'])
    attention_mask = torch.tensor(encodings_dict['attention_mask'])

    outputs = model.generate(input_ids=input_ids, attention_mask=attention_mask, do_sample=True, output_scores=True, return_dict_in_generate=True)
    translated = outputs.sequences
    scores = outputs.scores
    print ([tokenizer.decode(t, skip_special_tokens=True) for t in translated])
    print (scores.shape)