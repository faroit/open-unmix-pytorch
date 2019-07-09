import random
from pathlib import Path
import torch
import torch.utils.data
import numpy as np
import sys
import argparse
import tqdm


try:
    import soundfile as sf
except ImportError:
    soundfile = None

try:
    import torchaudio
except ImportError:
    torchaudio = None

try:
    import musdb
except ImportError:
    musdb = None


class Compose(object):
    """Composes several augmentation transforms.
    Args:
        augmentations: list of augmentations to compose.
    """

    def __init__(self, transforms):
        self.transforms = transforms

    def __call__(self, audio):
        for t in self.transforms:
            audio = t(audio)
        return audio


def augment_source_gain(audio):
    g = random.uniform(0.25, 1.25)
    return audio * g


def augment_source_channelswap(audio):
    if audio.shape[0] == 2 and random.random() < 0.5:
        return np.flip(audio, 0)
    else:
        return audio


def soundfile_info(path):
    info = {}
    sfi = sf.info(path)
    info['samplerate'] = sfi.samplerate
    info['samples'] = int(sfi.duration * sfi.samplerate)
    info['duration'] = sfi.duration
    return info


def soundfile_loader(path, start=0, dur=None):
    # get metadata
    info = soundfile_info(path)
    start = int(start * info['samplerate'])
    # check if dur is none
    if dur:
        # stop in soundfile is calc in samples, not seconds
        stop = start + int(dur * info['samplerate'])
    else:
        # set to None for reading complete file
        stop = dur

    audio, _ = sf.read(
        path,
        always_2d=True,
        start=start,
        stop=stop
    )
    return torch.FloatTensor(audio.T)


def torchaudio_info(path):
    # get length of file in samples
    info = {}
    si, _ = torchaudio.info(str(path))
    info['samplerate'] = si.rate
    info['samples'] = si.length // si.channels
    info['duration'] = info['samples'] / si.rate
    return info


def torchaudio_loader(path, start=0, dur=None):
    info = torchaudio_info(path)
    # loads the full track duration
    if dur is None:
        sig, rate = torchaudio.load(path)
        return sig
        # otherwise loads a random excerpt
    else:
        num_frames = int(dur * info['samplerate'])
        offset = int(start * info['samplerate'])
        sig, rate = torchaudio.load(
            path, num_frames=num_frames, offset=offset
        )
        return sig


def audioloader(path, start=0, dur=None):
    if 'torchaudio' in sys.modules:
        return torchaudio_loader(path, start=start, dur=dur)
    else:
        return soundfile_loader(path, start=start, dur=dur)


def audioinfo(path):
    if 'torchaudio' in sys.modules:
        return torchaudio_info(path)
    else:
        return soundfile_info(path)


def load_datasets(parser, args):
    if args.dataset == 'unaligned':
        parser.add_argument('--interferences', type=str, nargs="+")
        args = parser.parse_args()

        dataset_kwargs = {
            'root': Path(args.root),
            'seq_duration': args.seq_dur,
            'target': args.target,
            'interferences': args.interferences
        }

        train_dataset = UnalignedSources(split='train', **dataset_kwargs)
        valid_dataset = UnalignedSources(split='valid', **dataset_kwargs)

    elif args.dataset == 'aligned':
        parser.add_argument('--input-file', type=str)
        parser.add_argument('--output-file', type=str)

        args = parser.parse_args()
        # set output target to basename of output file
        args.target = Path(args.output_file).stem

        dataset_kwargs = {
            'root': Path(args.root),
            'seq_duration': args.seq_dur,
            'input_file': args.input_file,
            'output_file': args.output_file
        }

        train_dataset = AlignedSources(split='train', **dataset_kwargs)
        valid_dataset = AlignedSources(split='valid', **dataset_kwargs)

    elif args.dataset == 'mixedsources':
        parser.add_argument('--interferers', type=str, nargs='+')
        parser.add_argument('--target-file', type=str)

        args = parser.parse_args()

        dataset_kwargs = {
            'root': Path(args.root),
            'seq_duration': args.seq_dur,
            'interferers': args.interferers,
            'target_file': args.target_file
        }

        train_dataset = MixedSources(split='train', **dataset_kwargs)
        valid_dataset = MixedSources(split='valid', **dataset_kwargs)

    elif args.dataset == 'musdb':
        parser.add_argument('--is-wav', action='store_true', default=False,
                            help='flags wav version of the dataset')
        parser.add_argument('--samples-per-track', type=int, default=64)

        args = parser.parse_args()
        dataset_kwargs = {
            'root': args.root,
            'is_wav': args.is_wav,
            'subsets': 'train',
            'target': args.target,
            'download': args.root is None
        }

        source_augmentations = Compose(
            [augment_source_channelswap, augment_source_gain]
        )

        train_dataset = MUSDBDataset(
            split='train',
            samples_per_track=args.samples_per_track,
            seq_duration=args.seq_dur,
            source_augmentations=source_augmentations,
            random_track_mix=True,
            **dataset_kwargs
        )

        valid_dataset = MUSDBDataset(
            split='valid', samples_per_track=1, seq_duration=None,
            **dataset_kwargs
        )

    return train_dataset, valid_dataset, args


def random_product(*args, repeat=1):
    "Random selection from itertools.product(*args, **kwds)"
    pools = [tuple(pool) for pool in args] * repeat
    return tuple(random.choice(pool) for pool in pools)


class UnalignedSources(torch.utils.data.Dataset):
    def __init__(
        self,
        root,
        split='train',
        seq_duration=None,
        target='drums',
        interferences=['noise'],
        glob="*.wav",
        sample_rate=44100,
        nb_samples=1000,
    ):
        """A dataset of that assumes sources to be unaligned,
        organized in subfolders with the name of sources

        Example:
            -- Sample 1 ----------------------

            train/noise/10923.wav --+
                                    +--> mixed input
            train/vocals/1.wav -----+
            train/vocals/1.wav --------> output target

        Scales to a large amount of audio data.
        Uses pytorch' index based sample access
        """
        self.root = Path(root).expanduser()
        self.sample_rate = sample_rate
        if seq_duration <= 0:
            self.seq_duration = None
        else:
            self.seq_duration = seq_duration
        self.nb_samples = nb_samples
        self.glob = glob
        self.source_folders = interferences + [target]
        self.sources = self._get_paths()

    def __getitem__(self, index):
        input_tuple = random_product(*self.sources)
        sample_sources = list(map(self.load_audio, input_tuple))
        mix = torch.stack(sample_sources, dim=0).sum(dim=0)
        target = self.load_audio(input_tuple[-1])
        return mix, target

    def __len__(self):
        return self.nb_samples

    def load_audio(self, fp):
        # loads the full track duration
        return audioloader(fp, start=0, dur=self.seq_duration)

    def _get_paths(self):
        """Loads input and output tracks"""

        sources_paths = []
        for source_folder in self.source_folders:
            p = Path(self.root, self.split, source_folder)
            sources_paths.append(list(p.glob(self.glob)))

        return sources_paths


class AlignedSources(torch.utils.data.Dataset):
    def __init__(
        self,
        root,
        split='train',
        input_file='mixture.wav',
        output_file='vocals.wav',
        seq_duration=None,
        sample_rate=44100,
    ):
        """A dataset of that assumes folders with sources

        Example:
            -- Sample 1 ----------------------
            train/01/mixture.wav --> input target
            train/01/vocals.wav ---> output target
            -- Sample 2 -----------------------
            train/02/mixture.wav --> input target
            train/02/vocals.wav ---> output target

        Scales to a large amount of audio data.
        Uses pytorch' index based sample access
        """
        self.root = Path(root).expanduser()
        self.split = split
        self.sample_rate = sample_rate
        if seq_duration <= 0:
            self.seq_duration = None
        else:
            self.seq_duration = seq_duration
        self.random_excerpt = (split == 'train')
        # set the input and output files (accept glob)
        self.input_file = input_file
        self.output_file = output_file
        self.tuple_paths = list(self._get_paths())

    def __getitem__(self, index):
        input_path, output_path = self.tuple_paths[index]
        input_info = audioinfo(input_path)
        output_info = audioinfo(output_path)
        if self.random_excerpt:
            # use the minimum of x and y in case they differ
            duration = min(input_info['duration'], output_info['duration'])
            if duration < self.seq_duration:
                index = index - 1 if index > 0 else index + 1
                return self.__getitem__(index)
            # random start in seconds
            start = random.uniform(0, duration - self.seq_duration)
        else:
            start = 0
        try:
            X_audio = audioloader(
                input_path, start=start, dur=self.seq_duration
            )
            Y_audio = audioloader(
                output_path, start=start, dur=self.seq_duration
            )
        except RuntimeError:
            print("error in ", input_path, output_path)
            index = index - 1 if index > 0 else index + 1
            return self.__getitem__(index)

        if X_audio.shape[1] < int(self.seq_duration * input_info['samplerate']) or Y_audio.shape[1] < int(self.seq_duration * output_info['samplerate']):
            index = index - 1 if index > 0 else index + 1
            return self.__getitem__(index)
        return X_audio, Y_audio

    def __len__(self):
        return len(self.tuple_paths)

    def _get_paths(self):
        """Loads input and output tracks"""
        p = Path(self.root, self.split)
        for track_folder in p.iterdir():
            if track_folder.is_dir():
                input_path = list(track_folder.glob(self.input_file))
                output_path = list(track_folder.glob(self.output_file))
                # if both targets are available in the subfolder add them
                if input_path and output_path:
                    yield input_path[0], output_path[0]


class MixedSources(torch.utils.data.Dataset):
    def __init__(
        self,
        root,
        split='train',
        target_file='vocals.wav',
        interferers=['bass.wav', 'drums.wav'],
        seq_duration=None,
        sample_rate=44100,
        augmentations=None,
    ):
        """A dataset of that assumes folders with sources

        Example:
            -- Sample 1 ----------------------
            train/01/vocals.wav --> input target   \
            train/01/drums.wav --> input target  --+--> will be mixed
            train/01/bass.wav --> input target    /
            train/01/vocals.wav ---> output target
            -- Sample 2 -----------------------
            train/02/vocals.wav --> input target   \
            train/02/drums.wav --> input target  --+--> will be mixed
            train/02/bass.wav --> input target    /
            train/02/vocals.wav ---> output target

        Scales to a large amount of audio data.
        Uses pytorch' index based sampling
        """
        self.root = Path(root).expanduser()
        self.split = split
        self.sample_rate = sample_rate
        if seq_duration <= 0:
            self.seq_duration = None
        else:
            self.seq_duration = seq_duration
        self.random_excerpt = (split == 'train')
        self.augmentations = (split == 'train')
        # set the input and output files (accept glob)
        self.target_file = target_file
        self.interferers = interferers
        self.tracks = list(self._get_paths())

    def __getitem__(self, index):

        audio_sources = []
        sources = self.interferers + [self.target_file]
        for source in sources:
            if self.augmentations:
                track_dir = random.choice(self.tracks)
            else:
                track_dir = self.tracks[index]
            source_path = track_dir / source
            if source_path.exists():
                input_info = audioinfo(source_path)
                duration = input_info['duration']
                if duration < self.seq_duration:
                    index = index - 1 if index > 0 else index + 1
                    return self.__getitem__(index)
            else:
                return self.__getitem__(index)
            # random start in seconds
            start = random.uniform(0, duration - self.seq_duration)
            try:
                X_audio = audioloader(
                    source_path, start=start, dur=self.seq_duration
                )
                audio_sources.append(X_audio)
            except RuntimeError:
                print("error in ", source_path)
                index = index - 1 if index > 0 else index + 1
                return self.__getitem__(index)

        stems = torch.stack(audio_sources)
        # # apply linear mix over source index=0
        x_audio = stems.sum(0)
        y_audio = stems[-1]
        return x_audio, y_audio

    def __len__(self):
        return len(self.tracks)

    def _get_paths(self):
        """Loads input and output tracks"""
        p = Path(self.root, self.split)
        for track_folder in p.iterdir():
            if track_folder.is_dir():
                yield track_folder


class MUSDBDataset(torch.utils.data.Dataset):
    def __init__(
        self,
        target='vocals',
        root=None,
        download=False,
        is_wav=False,
        subsets='train',
        split='train',
        seq_duration=6.0,
        samples_per_track=64,
        source_augmentations=lambda audio: audio,
        random_track_mix=False,
        dtype=torch.float32,
        seed=42,
        *args, **kwargs
    ):
        """MUSDB18 torch.data.Dataset that samples from the MUSDB tracks
        using track and excerpts with replacement.

        Parameters
        ----------
        target : str
            target name of the source to be separated, defaults to ``vocals``.
        root : str
            root path of MUSDB
        download : boolean
            automatically download 7s preview version of MUSDB
        is_wav : boolean
            specify if the WAV version (instead of the MP4 STEMS) are used
        subsets : list-like [str]
            subset str or list of subset. Defaults to ``train``.
        split : str
            use (stratified) track splits for validation split (``valid``),
            defaults to ``train``.
        seq_duration : float
            training is performed in chunks of ``seq_duration`` (in seconds,
            defaults to ``None`` which loads the full audio track
        samples_per_track : int
            sets the number of samples that are yielded from each musdb track
            in one epoch. Defaults to 64
        source_augmentations : list[callables]
            provide list of augmentation function that take a multi-channel
            audio file of shape (src, samples) as input and output. Defaults to
            no-augmentations (input = output)
        random_track_mix : boolean
            randomly mixes sources from different tracks to assemble a
            custom mix. This augmenation is only applied for the train subset.
        seed : int
            control randomness of dataset iterations
        dtype : numeric type
            data type of torch output tuple x and y
        args, kwargs : additional keyword arguments
            used to add further control for the musdb dataset
            initialization function.

        """
        random.seed(seed)
        self.is_wav = is_wav
        self.seq_duration = seq_duration
        self.target = target
        self.subsets = subsets
        self.split = split
        self.samples_per_track = samples_per_track
        self.source_augmentations = source_augmentations
        self.random_track_mix = random_track_mix
        self.mus = musdb.DB(
            root=root,
            is_wav=is_wav,
            split=split,
            subsets=subsets,
            download=download,
            *args, **kwargs
        )
        self.sample_rate = 44100  # musdb is fixed sample rate
        self.dtype = dtype

    def __getitem__(self, index):
        audio_sources = []
        target_ind = None

        # select track
        track = self.mus.tracks[index // self.samples_per_track]

        # at training time we assemble a custom mix
        if self.split == 'train':
            for k, source in enumerate(self.mus.setup['sources']):
                # memorize index of target source
                if source == self.target:
                    target_ind = k

                # select a random track
                if self.random_track_mix:
                    track = random.choice(self.mus.tracks)

                # set the excerpt duration
                track.chunk_duration = self.seq_duration
                # set random start position
                track.chunk_start = random.uniform(
                    0, track.duration - self.seq_duration
                )
                # load source audio and apply time domain source_augmentations
                audio = torch.tensor(
                    self.source_augmentations(track.sources[source].audio.T),
                    dtype=self.dtype
                )
                audio_sources.append(audio)

            # create stem tensor of shape (source, channel, samples)
            stems = torch.stack(audio_sources)
            # # apply linear mix over source index=0
            x = stems.sum(0)
            # get the target stem
            if target_ind is not None:
                y = stems[target_ind]
            # assuming vocal/accompaniment scenario if target!=source
            else:
                vocind = list(self.mus.setup['sources'].keys()).index('vocals')
                # apply time domain subtraction
                y = x - stems[vocind]

        # for validation and test, we deterministically yield the full
        # pre-mixed musdb track
        else:
            # get the non-linear source mix straight from musdb
            x = torch.tensor(
                track.audio.T,
                dtype=self.dtype
            )
            y = torch.tensor(
                track.targets[self.target].audio.T,
                dtype=self.dtype
            )

        return x, y

    def __len__(self):
        return len(self.mus.tracks) * self.samples_per_track


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Open Unmix Trainer')
    parser.add_argument(
        '--dataset', type=str, default="musdb",
        choices=['musdb', 'aligned', 'unaligned', 'mixedsources'],
        help='Name of the dataset.'
    )

    parser.add_argument(
        '--root', type=str, help='root path of dataset'
    )

    parser.add_argument(
        '--save',
        action='store_true',
        help=('write out a fixed dataset of samples')
    )

    parser.add_argument('--target', type=str, default='vocals')

    # I/O Parameters
    parser.add_argument(
        '--seq-dur', type=float, default=5.0,
        help='Duration of <=0.0 will result in the full audio'
    )

    parser.add_argument('--batch-size', type=int, default=16)

    args, _ = parser.parse_known_args()
    train_dataset, valid_dataset, args = load_datasets(parser, args)

    train_sampler = torch.utils.data.DataLoader(
        train_dataset, batch_size=args.batch_size, shuffle=True, num_workers=0,
    )

    if args.save:
        for k, (x, y) in enumerate(train_dataset):
            torchaudio.save(
                "test/" + str(k) + 'x.wav',
                x,
                44100,
                precision=16,
                channels_first=True
            )
            torchaudio.save(
                "test/" + str(k) + 'y.wav',
                y,
                44100,
                precision=16,
                channels_first=True
            )

    # check datasampler
    for x, y in tqdm.tqdm(train_sampler):
        pass
