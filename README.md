# orpheusdl-amazonmusic
[OrpheusDL](https://github.com/yarrm80s/orpheusdl) module for downloading music from [Amazon Music](https://music.amazon.com/)

Written by: [reaitten](https://github.com/reaitten)

## Installation

1. Install [`shaka-packager`](https://github.com/shaka-project/shaka-packager/releases/latest), [`ffmpeg`](https://ffmpeg.org/), and [`aria2c`](https://github.com/aria2/aria2/releases/latest) (optional) onto your host computer.

2. Download or clone the source code into `modules`.
Example: `git clone https://github.com/reaitten/orpheusdl-amazonmusic modules/amazonmusic` 

> [!IMPORTANT]
> You must be inside your cloned installation of orpheusdl first.


It is recomended to use a [*virtual environment*](https://docs.python.org/3/library/venv.html). (not required)

3. Install the module specific requirements:\
```pip install -r modules/amazonmusic/requirements.txt```

4. Reload your settings file (`settings.json`) to load the newly added Amazon Music module

```console
python orpheus.py settings refresh
```

5. Inside `settings.json`, find the name of the cloned module (usually `amazon` or `amazonmusic`) and fill in each field.

# Module specifics settings

`wvd_path`: The full path to your dumped [.wvd / CDM](https://bunny.net/academy/streaming/what-is-widevine-cdm-content-decryption-module/).

To create a `.wvd` file using your newly dumped L3 CDM ([example](https://github.com/Diazole/dumper)):\
```pywidevine create-device --type ANDROID --level 3 --key "private_key.pem" --client_id "client_id.bin"```


<!-- TODO, complete the list of valid strings -->
`max_track_quality_to_use`: By default, the module checks the global `download_quality` inside your settings to choose your preferred maximum avaliable quality.

If `proprietary_codecs` inside your settings is set to `false`, no 3D audio will be downloaded regardless if `max_track_quality_to_use` is set to one of the spatial audio options.

You can set any one of this strings, as they are valid, as long as it is avaliable for streaming.

- `LD` - The lowest definition Amazon provides. typically in Opus.
    - `LD_MEDIUM`
    - `LD_LOW`
- `SD` - Standard Definition, typically in Opus. 
    - `SD_LOW`
    - `SD_MEDIUM`
    - `SD_HIGH`
- `HD` - High Definition, typically 16 bit in FLAC.
    - `HD_44`
- `UHD` - Ultra HD, typically 24 bit in FLAC.
    - `UHD_48`

## Spatial audio (Sony 360RA, Dolby Atmos)
- `SPATIAL_ATMOS` - Dolby Atmos (Codecs: E-AC-3 and/or AC-3)
    - `SPATIAL_ATMOS_LOW`
    - `SPATIAL_ATMOS_MEDIUM`
    - `SPATIAL_ATMOS_HIGH`

- `SPATIAL_RA360` - Sony 360RA (Codecs: MHM1 and MHA1)
    - `SPATIAL_RA360_L0`
    - `SPATIAL_RA360_L1`
    - `SPATIAL_RA360_L2`
    - `SPATIAL_RA360_L3`


## Changelog
Brief summary of modifications made to this module.

### v1.0.0
Initial version.

# Special thanks
- [audible](audible.readthedocs.io) library for creating the basic logic needed to login onto the mobile app
- [mitmproxy](https://mitmproxy.org/) for helping me understand the mobile app's inbound and outbound HTTP connections.
- [shaka-project](https://shaka-project.github.io/) for Widevine DRM decryption
- Amazon for using a **secure** DRM.