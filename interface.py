import base64
import json
import logging
import re
import time
from datetime import datetime
import typing
from urllib.parse import urlparse
from uuid import UUID, uuid1, uuid4

import dateutil.parser
import ffmpeg
import natsort
from Crypto.PublicKey import RSA
from pywidevine import PSSH, Cdm, Device, LicenseRequest
from pywidevine.license_protocol_pb2 import (ClientIdentification,
                                             DrmCertificate, FileHashes,
                                             SignedDrmCertificate)

from utils.models import *
from utils.utils import create_temp_filename, download_file, silentremove

from .az_web_api import AmazonWebAPI
from .azapi import AmazonMusicMobileAPI, AmazonMusicMobileAPICredentials

LOGGER = logging.getLogger(__name__)

@dataclass
class AudioTrack:
    asin: str
    codec: CodecEnum
    bitrate: int
    sample_rate: int
    url: str
    pssh: str
    quality: str
    quality_ranking: int
    bit_depth: Optional[int] = None

# This is a Amazon Music module for OrpheusDL, doesn't not require an active subscription

module_information = ModuleInformation(  # Only service_name and module_supported_modes are mandatory
    service_name="Amazon Music",
    module_supported_modes=ModuleModes.download
    | ModuleModes.lyrics,
    # | ModuleModes.covers,
    # | ModuleModes.credits,
    # flags = ModuleFlags.hidden,
    # Flags:
    # startup_load: load module on startup
    # hidden: hides module from CLI help options
    # jwt_system_enable: handles bearer and refresh tokens automatically, though currently untested
    # private: override any public modules, only enabled with the -p/--private argument, currently broken
    global_settings={},
    global_storage_variables=[],
    session_settings={
        "email": "",
        "password": "",
        "country": "",
        "country_tld": "",
        "wvd_path": "",
        "prefer_mha1": False
    },
    session_storage_variables=["credentials"],
    netlocation_constant="amazon",
    test_url="https://music.amazon.com/albums/B08TZPYLJN",
    # TODO use custom_url_parsing cause of the weird urls
    url_constants={  # This is the default if no url_constants is given. Unused if custom_url_parsing is flagged
        "track": DownloadTypeEnum.track,
        "album": DownloadTypeEnum.album,
        # 'playlist': DownloadTypeEnum.playlist,
        # 'artist': DownloadTypeEnum.artist
    },  # How this works: if '/track/' is detected in the URL, then track downloading is triggered
    login_behaviour=ManualEnum.manual,  # setting to ManualEnum.manual disables Orpheus automatically calling login() when needed
    url_decoding=ManualEnum.manual  # setting to ManualEnum.manual disables Orpheus' automatic url decoding which works as follows:
    # taking the url_constants dict as a list of constants to check for in the url's segments, and the final part of the URL as the ID
)


class ModuleInterface:
    def __init__(self, module_controller: ModuleController):
        self.settings = module_controller.module_settings
        self.module_controller = module_controller
        self.quality_parse = {
            QualityEnum.MINIMUM: [
                "LD",
            ],
            QualityEnum.LOW: [
                "SD_LOW",
            ],
            QualityEnum.MEDIUM: [
                "SD_MEDIUM",
            ],
            QualityEnum.HIGH: [
                "SD_HIGH",
            ],
            QualityEnum.LOSSLESS: [
                "HD",
                "UHD",
            ],
            QualityEnum.HIFI: [
                "SPATIAL_ATMOS",
                "SPATIAL_RA360"
            ],

        }
        # if not module_controller.orpheus_options.disable_subscription_check and (
        #     self.quality_parse[module_controller.orpheus_options.quality_tier]
        #     > self.mobile_session.get_user_tier()
        # ):
        #     print(
        #         "Example: quality set in the settings is not accessible by the current subscription"
        #     )
        
        device = Device.load(self.settings["wvd_path"])
        
        
        self.cdm = Cdm.from_device(device)

        creds = module_controller.temporary_settings_controller.read("credentials")
        credentials = AmazonMusicMobileAPICredentials(**creds) if creds else None
        
        self.mobile_session = AmazonMusicMobileAPI(
            credentials=credentials, country_code=self.settings["country"]
        )
        if not self.mobile_session.credentials:
            self.login_onto_mobile(self.settings["email"], self.settings["password"])
        
        self.web_session = AmazonWebAPI(self.mobile_session.credentials, dict(self.mobile_session.session.cookies))
        self.web_session.get_config(response_text=self.mobile_session.get_root(), country_tld=self.settings["country_tld"])

        module_controller.temporary_settings_controller.set(
            "credentials", self.mobile_session.credentials.to_dict()
        )
        LOGGER.debug(self.mobile_session._retrieve_capability())
        LOGGER.debug(self.mobile_session.credentials)

    def login_onto_mobile(
        self, email: str, password: str
    ):  # Called automatically by Orpheus when standard_login is flagged, otherwise optional
        credentials = self.mobile_session.login_via_mobile(
            email, password, self.settings["country_tld"], self.settings["country"]
        )
        if not credentials:
            raise Exception("Login failed")

        self.module_controller.temporary_settings_controller.set(
            "credentials", credentials.to_dict()
        )

    def custom_url_parse(self, link: str) -> MediaIdentification:
        url = urlparse(link)
        
        if not url.netloc.endswith(self.mobile_session.credentials.tld):
            raise ValueError(f"You must provide a URL that is within the same region as the account!")

        queries = url.query.split("&")
        print(url)
        # check if trying to download a track
        for query in queries:
            if query.startswith("trackAsin="):
                return MediaIdentification(
                    media_type=DownloadTypeEnum.track, media_id=query.split("=")[1]
                )

        components = url.path.split("/")

        # use the same logic as orpheusdl cuz lazy
        url_constants = {
            "albums": DownloadTypeEnum.album,
            "playlists": DownloadTypeEnum.playlist,
            "artists": DownloadTypeEnum.artist,
        }

        type_matches = [
            media_type
            for url_check, media_type in url_constants.items()
            if url_check in components
        ]

        return MediaIdentification(
            media_type=type_matches[-1], media_id=components[-1]
        )

    def get_track_info(
        self,
        track_id: str,
        quality_tier: QualityEnum,
        codec_options: CodecOptions,
        data={},
    ) -> TrackInfo:  # Mandatory
        try:
            if self.mobile_session.credentials.access_token_expired:
                self.mobile_session.refresh_access_token()

            # quality_to_use = self.quality_parse[quality_tier]
            
            track_data = (
                data[track_id]
                if data and track_id in data
                else self.mobile_session.get_track_info(track_id)
            )
            album_id = str(track_data["album"]["asin"])
            album_data = (
                data[album_id]
                if data and album_id in data
                else self.mobile_session.get_album_info(album_id)
            )
            search_data = (
                data[f"{album_id}_search"]
                if data and f"{album_id}_search" in data
                else self.mobile_session.search(
                    query=f"{album_data['title']} - {track_data['title']}",
                    asin=track_id,
                    search_types=["catalog_track"]
                )
            )
            
            # TODO, unused for now
            # artists = self.mobile_session.get_metadata(
            #     [track_data["artist"]["asin"], *track_data["artist"]["contributorAsins"]], self.settings["country"]
            # )["artistList"]
            
            release_datetime = self._get_date_from_metadata(album_data)
            mpd = dict(self.mobile_session.get_track_manifest(track_id))
            # LOGGER.debug(json.dumps(mpd, indent=3))
            avaliable_tracks = self._parse_track_mpd(mpd, track_id)
            
            # filter out spatial audio, if specified
            if not codec_options.spatial_codecs:
                avaliable_tracks = list(filter(lambda c: not codec_data[c.codec].spatial, avaliable_tracks))
            if not self.settings['prefer_mha1']:
                avaliable_tracks = list(filter(lambda c: c.codec.value != CodecEnum.MHA1.value, avaliable_tracks))
            
            LOGGER.debug(list(map(lambda x: x.quality, avaliable_tracks)))
            LOGGER.debug(avaliable_tracks)

            avaliable_qualities_enum = list(
                k
                for k in self.quality_parse.keys()
                if k.value <= quality_tier.value
            )
            LOGGER.debug(avaliable_qualities_enum)
            # Select the highest quality avaliable, start iterating at max first
            # NOTE there *could* be a different way of doing this, i'm just stupid af
            track_to_use = None
            for qualities, oquality in zip(reversed(self.quality_parse.values()), reversed(self.quality_parse)):
                if oquality not in avaliable_qualities_enum:
                    continue
                for quality in qualities:
                    for item in natsort.natsorted(filter(lambda c: c.quality.startswith(quality), avaliable_tracks), key=lambda x: x.quality_ranking):
                        if not item.quality.startswith(quality):
                            continue
                        track_to_use = item
                        break
                if track_to_use is not None:
                    break
                
            LOGGER.debug(f"Using AudioTrack: {track_to_use}")
            
            # Amazon Music doesn't have any "Disc" seperation, so it is typically assumed to be 1 however:
            disc_total = max(int(t['discNum']) for t in album_data.get('tracks', [{}]))
            composers = "; ".join(
                natsort.natsorted(
                    track_data.get("songWriters", [album_data["primaryArtistName"]])
                )
            )
            comment = f"https://music.amazon.{self.mobile_session.credentials.tld}/albums/{album_id}"

            extra_tags = {
                "Merchant": " ".join(str(album_data["productDetails"]["merchantName"]).split())
                if album_data.get("productDetails", {}).get("merchantName")
                else None,
                "Composer": composers, # force set the composer tag, because orpheus doesn't handle it
            }

            tags = Tags(  # every single one of these is optional
                album_artist=album_data["primaryArtistName"],
                composer=composers,
                copyright=album_data["productDetails"]["copyright"],
                isrc=track_data["isrc"],
                # upc="",
                disc_number=int(track_data["discNum"] or 1),
                total_discs=disc_total,
                track_number=int(track_data["trackNum"] or 1),  # None/0/1 if no discs
                total_tracks=int(album_data["trackCount"] or 1),  # None/0/1 if no discs
                # replay_gain=0.0,
                # replay_peak=0.0,
                genres=[album_data["productDetails"]["primaryGenreName"]],
                label=album_data["productDetails"]["label"],
                release_date=release_datetime.strftime("%Y-%m-%d"),  # Format: YYYY-MM-DD
                comment=comment,
                extra_tags=extra_tags
            )
            
            artwork_url = search_data['artOriginal']['artUrl']
            # if self.mobile_session.country_code == "AU":
            #     # specific error with AU, httpcore.ConnectError: [Errno -2] Name or service not known
            #     # Not sure how this happens
            #     artwork_url = None

            return TrackInfo(
                name=track_data["title"],
                album_id=album_id,
                album=track_data["album"]["title"],
                artists=[album_data["artist"]["name"]],
                tags=tags,
                codec=track_to_use.codec,
                cover_url=artwork_url,  # make sure to check module_controller.orpheus_options.default_cover_options
                release_year=release_datetime,
                explicit=track_data["parentalControls"]["hasExplicitLanguage"],
                artist_id=track_data["artist"]["asin"],  # optional
                duration=track_data["duration"],
                # animated_cover_url="",  # optional
                # description="",  # optional
                bit_depth=track_to_use.bit_depth,  # optional
                sample_rate=track_to_use.sample_rate,  # optional
                # bitrate=1411,  # optional
                download_extra_kwargs={
                    "audio_track": track_to_use,
                },  # optional only if download_type isn't DownloadEnum.TEMP_FILE_PATH, whatever you want
                # cover_extra_kwargs={
                #     "data": {track_id: ""}
                # },  # optional, whatever you want, but be very careful
                # credits_extra_kwargs={
                #     "data": {track_id: ""}
                # },  # optional, whatever you want, but be very careful
                # lyrics_extra_kwargs={
                #     "data": {track_id: ""}
                # },  # optional, whatever you want, but be very careful
                error="",  # only use if there is an error
            )
        except Exception as e:
            LOGGER.debug(e, exc_info=1)

    # def get_track_download(self, file_url: str, codec: CodecEnum, pssh: PSSH, **kwargs):
    def get_track_download(self, audio_track: AudioTrack, **kwargs):
        encrypted_track_location = f"{create_temp_filename()}.mp4"

        download_file(
            audio_track.url,
            encrypted_track_location,
            enable_progress_bar=True,
        )

        # decrypt the file (attempt to request a license)
        session_id = self.cdm.open()
        try:
            # self.cdm.set_service_certificate(session_id, None)
            license_challenge = base64.b64encode(self.cdm.get_license_challenge(
                session_id, audio_track.pssh, privacy_mode=False
            )).decode("utf-8")

            license_response = self.web_session.get_license_response(license_challenge)
            if not license_response:
                license_response = input("License retrieval failed, enter response here: ")
            self.cdm.parse_license(session_id, license_response)

            decrypted_track_location = f"{create_temp_filename()}.mp4" #{codec_data[audio_track.codec].container.name}
            os.makedirs("temp/", exist_ok=True)

            self.cdm.decrypt(session_id, encrypted_track_location, decrypted_track_location, exists_ok=True)
            LOGGER.debug("Ok wth decryption")
            
            selected_codec_data = codec_data[audio_track.codec]
            
            if selected_codec_data.spatial:
                return TrackDownloadInfo(
                    download_type=DownloadEnum.TEMP_FILE_PATH,
                    temp_file_path=decrypted_track_location,
                    different_codec=audio_track.codec
                )

            LOGGER.debug(f"Using {selected_codec_data.container.name} as the container for {audio_track.codec.name}")
            final_decrypted_track_location = f"{create_temp_filename()}.{selected_codec_data.container.name}"
            ffmpeg.input(decrypted_track_location).output(final_decrypted_track_location, loglevel='warning', audio_bitrate=audio_track.bitrate).run()

        except Exception as e:
            print(e)
            self.cdm.close(session_id)
            silentremove(encrypted_track_location)
            return

        return TrackDownloadInfo(
            download_type=DownloadEnum.TEMP_FILE_PATH,
            temp_file_path=final_decrypted_track_location
        )

    def get_album_info(
        self, album_id: str, data={}
    ) -> Optional[AlbumInfo]:  # Mandatory if ModuleModes.download
        # raise NotImplementedError
        LOGGER.debug("Getting album info")

        try:
            album_data = (
                data[album_id]
                if album_id in data
                else self.mobile_session.get_album_info(album_id)
            )
            # Force use the ASIN the API returns with
            album_id = album_data["asin"]
            search_data = (
                data[f"{album_id}_search"]
                if data and f"{album_id}_search" in data
                else self.mobile_session.search(
                    query=f"{album_data['artist']['name']} - {album_data['title']}",
                    asin=album_id,
                    limit=100
                )
            )
            ai = AlbumInfo(
                name=album_data.get("title", "Unknown"),
                artist=album_data.get("primaryArtistName"),
                tracks=[track['asin'] for track in album_data.get("tracks", [])],
                release_year=self._get_date_from_metadata(album_data).strftime("%Y"),
                # explicit=False, #TODO iterate over tracks and use any()
                duration=album_data.get("duration"),
                artist_id=album_data.get("artist", {}).get("asin"),  # optional
                # booklet_url="",  # optional
                cover_url=search_data.get("artOriginal", {}).get("artUrl", album_data.get("image")),  # optional
                cover_type=ImageFileTypeEnum.jpg,  # optional
                all_track_cover_jpg_url="",  # technically optional, but HIGHLY recommended
                animated_cover_url="",  # optional
                description="",  # optional
                track_extra_kwargs={"data": {track['asin']: track for track in album_data.get("tracks", [])} | {album_id: album_data} | {f"{album_id}_search": search_data}},  # optional, whatever you want
            )
            LOGGER.debug(ai)
            return ai
        except Exception as e:
            LOGGER.error(e, exc_info=1)

    def get_playlist_info(
        self, playlist_id: str, data={}
    ) -> PlaylistInfo:  # Mandatory if either ModuleModes.download or ModuleModes.playlist
        raise NotImplementedError
        playlist_data = (
            data[playlist_id]
            if playlist_id in data
            else self.mobile_session.get_playlist(playlist_id)
        )

        return PlaylistInfo(
            name="",
            creator="",
            tracks=[],
            release_year="",
            explicit=False,
            creator_id="",  # optional
            cover_url="",  # optional
            cover_type=ImageFileTypeEnum.jpg,  # optional
            animated_cover_url="",  # optional
            description="",  # optional
            track_extra_kwargs={"data": ""},  # optional, whatever you want
        )

    def get_artist_info(
        self, artist_id: str, get_credited_albums: bool
    ) -> ArtistInfo:  # Mandatory if ModuleModes.download
        # get_credited_albums means stuff like remix compilations the artist was part of
        artist_data = self.mobile_session.get_artist(artist_id)

        return ArtistInfo(
            name="",
            albums=[],  # optional
            album_extra_kwargs={"data": ""},  # optional, whatever you want
            tracks=[],  # optional
            track_extra_kwargs={"data": ""},  # optional, whatever you want
        )

    def get_track_credits(
        self, track_id: str, data={}
    ):  # Mandatory if ModuleModes.credits
        track_data = (
            data[track_id] if track_id in data else self.mobile_session.get_track(track_id)
        )
        credits = track_data["credits"]
        credits_dict = {}
        return [CreditsInfo(k, v) for k, v in credits_dict.items()]

    def get_track_cover(
        self, track_id: str, cover_options: CoverOptions, data={}
    ) -> CoverInfo:  # Mandatory if ModuleModes.covers
        track_data = dict(
            (
                data[track_id]
                if track_id in data
                else self.mobile_session.get_track_info(track_id)
            )
        )
        print(self.mobile_session.get_track_info(track_id))
        cover_url: str | None = str(track_data.get("album", {}).get("image") or None)
        return CoverInfo(
            url=cover_url,
            file_type=ImageFileTypeEnum.jpg
            if cover_url and cover_url.endswith("jpg")
            else None,
        )

    def get_track_lyrics(
        self, track_id: str, data={}
    ) -> LyricsInfo:  # Mandatory if ModuleModes.lyrics
        track_lyrics_resp = self.mobile_session.get_track_lyrics(track_id)

        embedded_lyrics = ""
        synced_lyrics = ""
        if int(track_lyrics_resp.get("lyricsResponseCode", 0)) == 1002:
            for line in track_lyrics_resp.get("lyrics", {"lines": []}).get("lines"):
                text = line["text"]
                
                start_time = int(line["startTime"])
                start_time_str = self.milliseconds_to_lrc_time(start_time)
                
                embedded_lyrics += f"[{start_time_str}] {text}\n"
                synced_lyrics += f"[{start_time_str}]{text}\n"
        
        
        return LyricsInfo(embedded=embedded_lyrics, synced=synced_lyrics)  # both optional if not found

    def search(
        self,
        query_type: DownloadTypeEnum,
        query: str,
        track_info: TrackInfo = None,
        limit: int = 10,
    ):  # Mandatory
        results = {}
        if track_info and track_info.tags.isrc:
            results = self.mobile_session.search(query_type.name, track_info.tags.isrc, limit)
        if not results:
            results = self.mobile_session.search(query_type.name, query, limit)

        return [
            SearchResult(
                result_id="",
                name="",  # optional only if a lyrics/covers only module
                artists=[],  # optional only if a lyrics/covers only module or an artist search
                year="",  # optional
                explicit=False,  # optional
                additional=[],  # optional, used to convey more info when using orpheus.py search (not luckysearch, for obvious reasons)
                extra_kwargs={
                    "data": {f"{i['id']}_search": i}
                }  # optional, whatever you want. NOTE: BE CAREFUL! this can be given to:
                # get_track_info, get_album_info, get_artist_info with normal search results, and
                # get_track_credits, get_track_cover, get_track_lyrics in the case of other modules using this module just for those.
                # therefore, it's recommended to choose something generic like 'data' rather than specifics like 'cover_info'
                # or, you could use both, keeping a data field just in case track data is given, while keeping the specifics, but that's overcomplicated
            )
            for i in results
        ]
    
    # helpers

    @staticmethod
    def _get_date_from_metadata(album_data: dict[str, typing.Any]):
        return datetime.fromtimestamp(int(str(album_data["originalReleaseDate"] or album_data["merchantReleaseDate"])) / 1000)

    @staticmethod
    def milliseconds_to_lrc_time(milliseconds: int):
        # Convert milliseconds to the proper LRC time format [mm:ss.xx]
        return f"{milliseconds // 60000:02}:{(milliseconds // 1000) % 60:02}.{milliseconds % 1000:03}"

    def _parse_track_mpd(self, mpd: dict, track_asin: str):
        manifest = dict(mpd['MPD']['Period'])
        avaliable_tracks: list[AudioTrack] = []
        # iterate over the manifest (which is a xml file that follows the urn:mpeg:dash:profile:isoff-on-demand:2011 profile to grab the tracks and append them to avaliable_tracks as a AudioTrack object
        # for period in manifest.findall("Period"):
        # for period in manifest.get("Period"):
        for adaptation_set in manifest.get("AdaptationSet"):
            adaptation_set: list
            # LOGGER.debug(json.dumps(adaptation_set, indent=3))
            content_type = adaptation_set.get("@contentType")
            if content_type != "audio":
                raise ValueError("Only supports audio MPDs!")
            # # use the correct quality type requested, NEEDS TO BE FIXED FOR (SD_LOW, SD_MEDIUM, SD_HIGH AND SPATIAL_AUDIO)
            # for supplemental_property in adaptation_set.findall(
            #     "SupplementalProperty"
            # ):
            #     if supplemental_property.get(
            #         "schemeIdUri"
            #     ) == "amz-music:trackType" and supplemental_property.get(
            #         "value"
            #     ).startswith(
            #         self.quality_parse[quality_tier]
            #     ):
            #         track_type = supplemental_property.get("value")
            #         break
            # else:
            #     continue  

            key_id = None
            pssh = None
            # print(dict(adaptation_set.items()))
            
            # print(adaptation_set.get("ContentProtection"))
            for content_property in adaptation_set.get("ContentProtection"):
                # print(content_property.attrib)
                # print(content_property)
                if (
                    content_property.get("@schemeIdUri")
                    == "urn:mpeg:dash:mp4protection:2011"
                ):
                    key_id = str(content_property.get("@cenc:default_KID"))
                    continue

                # NOTE might need to use xmltodict instead of xml.etree.ElementTree
                if content_property.get("@schemeIdUri") == "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed":
                    LOGGER.debug(f"Chosen: {adaptation_set}")
                    pssh = PSSH(content_property.get("cenc:pssh"), strict=True)
                    # print(f"{pssh=}")
                    break

            if not key_id or not pssh:
                print("failed")
                raise ValueError("No key id or PSSH found!")

            # unused, can be used for album.quality but can't move
            # codec_data[codec].spatial
            supplemental_property = adaptation_set.get("SupplementalProperty")
            supprop = supplemental_property if isinstance(supplemental_property, list) else [supplemental_property]
            official_quality_name = [
                item.get('@value')
                for item in supprop
                if item.get("@schemeIdUri") == "amz-music:trackType"
            ][0]
            LOGGER.debug(f"Official name for track: {official_quality_name}")
            
            # print(adaptation_set)
            representations = [adaptation_set.get("Representation")] if isinstance(adaptation_set.get("Representation"), dict) else list(adaptation_set.get("Representation"))
            for representation in representations:
                # LOGGER.debug(f"{representation=}")
                media_url = str(representation.get("BaseURL"))
                
                muq = iter(re.split('=|&', urlparse(media_url).query))
                media_url_query = dict(zip(muq, muq))
                LOGGER.debug(media_url_query)
                quality = media_url_query.get("ql")
                
                codec = str(representation.get("@codecs")).upper()
                # 360 Audio
                if codec.startswith("MHA1"):
                    codec = "MHA1"
                elif codec.startswith("MHM1"):
                    codec = "MHM1"
                # Dolby Atmos
                elif codec.startswith("EC-3"):
                    codec = "EAC3"
                elif codec.startswith("AC-4"):
                    codec = "AC4"

                codec = CodecEnum[codec]
                # LOGGER.debug(codec)

                avaliable_tracks.append(
                    AudioTrack(
                        asin=track_asin,
                        codec=codec,
                        bit_depth=int(
                            representation.get("SupplementalProperty", {}).get("@value", 0)
                        ) if not (codec_data[codec].spatial and codec_data[codec].proprietary) else None,
                        bitrate=int(
                            representation.get("@bandwidth") or 0
                        ),  # bandwidth is a period; cps. multiply by 2
                        sample_rate=int(
                            representation.get("@audioSamplingRate") or 0
                        ),
                        # url=representation.find("BaseURL").text,
                        url=media_url,
                        quality_ranking=int(representation.get("@qualityRanking")),
                        quality=quality,
                        pssh=pssh,
                    )
                )
                    
        if not avaliable_tracks:
            raise ValueError("No tracks found!")

        avaliable_tracks = natsort.natsorted(avaliable_tracks, key=lambda x: x.quality, reverse=False)
        return avaliable_tracks