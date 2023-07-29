# orpheusdl-amazonmusic
[OrpheusDL](https://github.com/yarrm80s/orpheusdl) module for downloading music from [Amazon Music](https://music.amazon.com/)

Written by: [reaitten](https://github.com/reaitten)

## Install

Install `shaka-packager` and `ffmpeg` onto your host computer.\
Download or clone the source code into `modules`.

It is recomended to use a [*virtual environment*](https://docs.python.org/3/library/venv.html). (not required)\
To install requirements:\
```pip install -r requirements.txt```

Update ```config/settings.json``` with Amazon Music settings:\
```python orpheus.py```

Fill in all the fields under `orpheusdl-amazondl` as they are all required.

To create a `.wvd` file:\
```pywidevine create-device --type ANDROID --level 3 --key "private_key.pem" --client_id "client_id.bin"```\
The full path to the newly generated `.wvd` is the value to enter in `settings.json`.

## Changelog
Brief summary of modifications made to this module.

### v1.0.0
Initial version.

# Special thanks
- [audible](audible.readthedocs.io) library for creating the basic logic needed to login onto the mobile app
- [mitmproxy](https://mitmproxy.org/) for helping me understand the mobile app's inbound and outbound HTTP connections.
- [shaka-project](https://shaka-project.github.io/) for Widevine DRM decryption
- Amazon for using a **secure** DRM.