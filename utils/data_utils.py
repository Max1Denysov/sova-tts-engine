"""
BSD 3-Clause License

Copyright (c) 2018, NVIDIA Corporation
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

* Redistributions of source code must retain the above copyright notice, this
  list of conditions and the following disclaimer.

* Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.

* Neither the name of the copyright holder nor the names of its
  contributors may be used to endorse or promote products derived from
  this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""
import os
from itertools import chain

import numpy as np
import librosa
import torch
import torch.utils.data
from scipy.io.wavfile import read
from tps import Handler
from tps.utils import prob2bool
import tps.symbols as symb

from modules import layers
from utils.utils import load_filepaths_and_text, Inputs, InputsCTC
from modules.loss_function import AttentionTypes


ctc_mapping = {
    symb.Languages.en: symb.LETTERS_EN,
    symb.Languages.ru: symb.LETTERS_RU,
    symb.Languages.ru_trans: symb.LETTERS_RU_TRANS
}


def get_ctc_symbols(language):
    return ctc_mapping[language] + ["_"]


def get_ctc_symbols_length(language):
    language = symb.Languages[language]
    return len(get_ctc_symbols(language))


class CustomSampler(torch.utils.data.Sampler):
    def __init__(self, data_source, batchsize, shuffle=False, optimize=False, len_diff=10):
        idxs = tuple(range(len(data_source.data)))

        self.optimize = optimize
        self.shuffle = shuffle
        self.batchsize = batchsize
        self.optimized_idxs = []

        if self.optimize:
            text_lengths = tuple(len(elem[1]) for elem in data_source.data)
            lengths_idxs_pairs = tuple(zip(text_lengths, idxs))

            lengths_idxs_pairs = sorted(lengths_idxs_pairs, key=lambda elem: elem[0])

            min_length = lengths_idxs_pairs[0][0]

            len_idxs = []
            min_len = min_length
            max_len = min_len + len_diff
            for j, (length, idx) in enumerate(lengths_idxs_pairs):
                if min_len <= length < max_len:
                    len_idxs.append(idx)
                    if j + 1 == len(lengths_idxs_pairs) and len_idxs:
                        self.optimized_idxs.append(len_idxs)
                else:
                    self.optimized_idxs.append(len_idxs)
                    len_idxs = [idx]
                    min_len = length
                    max_len = min_len + len_diff

            idxs = tuple(chain(*self.optimized_idxs))

        self.idxs = idxs

        if self.shuffle:
            self.reshuffle()


    def __iter__(self):
        for i in self.idxs:
            yield i

        if self.shuffle:
            self.reshuffle()


    def __len__(self):
        return len(self.idxs)


    def reshuffle(self):
        def _torch_shuffle(iterable):
            return tuple(iterable[i] for i in torch.randperm(len(iterable)).tolist())


        idxs = tuple(_torch_shuffle(elem) for elem in self.optimized_idxs) if self.optimize else self.idxs
        idxs = _torch_shuffle(idxs)

        if self.optimize:
            idxs = list(chain(*idxs))

            batches = len(idxs) // self.batchsize
            idxs = tuple(idxs[i * self.batchsize:(i + 1) * self.batchsize] for i in range(batches))
            idxs = _torch_shuffle(idxs)

            idxs = list(chain(*idxs))

        self.idxs = idxs


class TextMelLoader(torch.utils.data.Dataset):
    def __init__(self, filelist_path, hparams):
        self.data = load_filepaths_and_text(filelist_path)
        self.audio_path = hparams.audios_path
        self.alignment_path = hparams.alignments_path

        self.add_silence = hparams.add_silence
        self.hop_length = hparams.hop_length
        self.ft_window = hparams.filter_length
        self.trim_silence = hparams.trim_silence
        self.trim_top_db = hparams.trim_top_db

        self.text_cleaners = hparams.text_cleaners
        self.max_wav_value = hparams.max_wav_value
        self.sampling_rate = hparams.sampling_rate
        self.load_mel_from_disk = hparams.load_mel_from_disk

        self.stft = layers.TacotronSTFT(
            hparams.filter_length, hparams.hop_length, hparams.win_length,
            hparams.n_mel_channels, hparams.sampling_rate, hparams.mel_fmin,
            hparams.mel_fmax)

        self.get_alignments = hparams.guided_attention_type == AttentionTypes.prealigned
        self.word_level_prob = hparams.word_level_prob
        self.stress = hparams.stress
        self.phonemes = hparams.phonemes
        self.dict_prime = hparams.dict_prime

        self.text_handler = Handler(hparams.language, hparams.dict_path)

        self.use_mmi = hparams.use_mmi
        self.ctc_symbol_to_id = None
        if hparams.use_mmi:
            self.ctc_symbol_to_id = {s: i for i, s in enumerate(get_ctc_symbols(hparams.language))}


    def __getitem__(self, index):
        return self.get_data(self.data[index])


    def __len__(self):
        return len(self.data)


    def _get_sress_phonemes(self):
        if not self.word_level_prob:
            phonemes = prob2bool(self.phonemes) if isinstance(self.phonemes, (int, float)) else self.phonemes
            stress = prob2bool(self.stress) if isinstance(self.stress, (int, float)) else self.stress
        else:
            phonemes = self.phonemes
            stress = self.stress

        return stress, phonemes


    # get mel and text
    def get_data(self, sample):
        stress, phonemes = self._get_sress_phonemes()
        audio_name, text = sample

        sequence = self.get_text(text, stress, phonemes)
        mel = self.get_mel(audio_name)

        alignment = None
        if self.get_alignments:
            assert not self.word_level_prob and not self.add_silence
            alignment = self.get_alignment(audio_name, stress, phonemes)
            # TODO: поправить эту хрень с alignment
            if alignment is None or (mel.size(1), sequence.size(0)) != alignment.shape:
                print("Some problems with {}: expected {} shape, got {}".format(audio_name,
                                                                                (mel.size(1), sequence.size(0)),
                                                                                alignment.shape))
                alignment = np.zeros(shape=(mel.shape[1], sequence.shape[0]))

            alignment = torch.FloatTensor(alignment)

        ctc_sequence = None
        if self.use_mmi:
            ctc_sequence = self.get_ctc_text(sequence.data.cpu().numpy())

        return sequence, mel, alignment, ctc_sequence


    def get_text(self, text, stress, phonemes):
        stress_always = not self.get_alignments
        text_norm = torch.IntTensor(
            self.text_handler.text_to_sequence(text, self.text_cleaners, stress, phonemes, self.dict_prime,
                                               stress_always)
        )
        return text_norm


    def get_audio(self, filename, trim_silence=False, add_silence=False):
        filepath = os.path.join(self.audio_path, filename)

        sample_rate, audio = read(filepath)
        audio = np.float32(audio / self.max_wav_value)  # faster than loading using librosa

        if sample_rate != self.sampling_rate:
            raise ValueError("{} SR doesn't match target {} SR".format(sample_rate, self.sampling_rate))

        audio_ = audio.copy()

        if trim_silence:
            idxs = librosa.effects.split(
                audio_,
                top_db=self.trim_top_db,
                frame_length=self.ft_window,
                hop_length=self.hop_length
            )

            audio_ = np.concatenate([audio_[start:end] for start, end in idxs])

        if add_silence:
            audio_ = np.append(audio_, np.zeros(5 * self.hop_length))

        audio_ = torch.FloatTensor(audio_.astype(np.float32))
        audio_ = audio_.unsqueeze(0)
        audio_ = torch.autograd.Variable(audio_, requires_grad=False)

        return audio_


    def get_mel_from_audio(self, audio):
        melspec = self.stft.mel_spectrogram(audio)
        return torch.squeeze(melspec, 0)


    def get_mel(self, filename):
        if not self.load_mel_from_disk:
            audio = self.get_audio(filename, self.trim_silence, self.add_silence)
            melspec = self.stft.mel_spectrogram(audio)
            melspec = torch.squeeze(melspec, 0)
        else:
            filepath = os.path.join(self.audio_path, filename)
            melspec = torch.from_numpy(np.load(filepath))
            assert melspec.size(0) == self.stft.n_mel_channels, (
                'Mel dimension mismatch: given {}, expected {}'.format(
                    melspec.size(0), self.stft.n_mel_channels))

        return melspec


    def get_alignment(self, audio_name, stress, phonemes):
        audio_name, _ = os.path.splitext(audio_name)
        alignment_name = audio_name + ".npy"

        if phonemes:
            return None  # у нас пока нет выравниваний для фонемного представления

        sub_dir = "original" if not stress else "stressed"
        filepath = os.path.join(self.alignment_path[sub_dir], alignment_name)

        return np.load(filepath)


    def get_ctc_text(self, sequence):
        text = [self.text_handler._id_to_symbol[s] for s in sequence]
        return torch.IntTensor([self.ctc_symbol_to_id[s] for s in text if s in self.ctc_symbol_to_id])


class TextMelCollate:
    def __init__(self, n_frames_per_step):
        self.n_frames_per_step = n_frames_per_step


    def __call__(self, batch):
        """Collate's training batch from normalized text and mel-spectrogram
        PARAMS
        ------
        batch: [text_normalized, mel_normalized]
        """
        # Right zero-pad all one-hot text sequences to max input length
        get_alignment = not any(elem[2] is None for elem in batch)
        get_ctc_text = not any(elem[3] is None for elem in batch)

        batchsize = len(batch)

        input_lengths, ids_sorted_decreasing = torch.sort(
            torch.LongTensor([len(x[0]) for x in batch]),
            dim=0, descending=True)
        max_input_len = input_lengths[0]
        max_target_len = max([x[1].size(1) for x in batch])
        num_mels = batch[0][1].size(0)

        text_padded = torch.LongTensor(batchsize, max_input_len)
        text_padded.zero_()

        mel_padded = torch.FloatTensor(batchsize, num_mels, max_target_len)
        mel_padded.zero_()

        gate_padded = torch.FloatTensor(batchsize, max_target_len)
        gate_padded.zero_()

        alignments_padded = None
        if get_alignment:
            alignments_padded = torch.FloatTensor(batchsize, max_target_len, max_input_len)
            alignments_padded.zero_()

        ctc_text_padded = None
        ctc_text_lengths = None
        if get_ctc_text:
            max_ctc_text_len = max([len(x[3]) for x in batch])

            ctc_text_padded = torch.LongTensor(batchsize, max_ctc_text_len)
            ctc_text_padded.zero_()

            ctc_text_lengths = torch.LongTensor(batchsize)

        output_lengths = torch.LongTensor(batchsize)

        for i, idx in enumerate(ids_sorted_decreasing):
            text, mel, alignment, ctc_text = batch[idx]

            in_len = text.size(0)
            target_len = mel.size(1)
            output_lengths[i] = target_len

            text_padded[i, :in_len] = text
            mel_padded[i, :, :target_len] = mel
            gate_padded[i, target_len - 1:] = 1

            if get_alignment:
                alignments_padded[i, :target_len, :in_len] = alignment

            if get_ctc_text:
                ctc_txt_len = ctc_text.size(0)
                ctc_text_lengths[i] = ctc_txt_len

                ctc_text_padded[i, :ctc_txt_len] = ctc_text

        # # Right zero-pad mel-spec
        # if max_target_len % self.n_frames_per_step != 0:
        #     max_target_len += self.n_frames_per_step - max_target_len % self.n_frames_per_step
        #     assert max_target_len % self.n_frames_per_step == 0

        inputs = Inputs(text=text_padded, mels=mel_padded, gate=gate_padded,
                        text_len=input_lengths, mel_len=output_lengths)

        inputs_ctc = InputsCTC(text=ctc_text_padded, length=ctc_text_lengths) if get_ctc_text else None

        return inputs, alignments_padded, inputs_ctc