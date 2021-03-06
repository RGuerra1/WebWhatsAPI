"""
WebWhatsAPI module

.. moduleauthor:: Mukul Hase <mukulhase@gmail.com>, Adarsh Sanjeev <adarshsanjeev@gmail.com>

"""

import binascii
import logging
from datetime import datetime, timedelta
from json import dumps, loads

import os
import shutil
import tempfile
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from axolotl.kdf.hkdfv3 import HKDFv3
from axolotl.util.byteutil import ByteUtil
from base64 import b64decode
from io import BytesIO
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .objects.chat import UserChat, factory_chat
from .objects.contact import Contact
from .objects.message import MessageGroup, factory_message
from .wapi_js_wrapper import WapiJsWrapper

__version__ = '2.0.3'


class WhatsAPIDriverStatus(object):
    Unknown = 'Unknown'
    NoDriver = 'NoDriver'
    NotConnected = 'NotConnected'
    NotLoggedIn = 'NotLoggedIn'
    LoggedIn = 'LoggedIn'


class WhatsAPIException(Exception):
    pass


class ChatNotFoundError(WhatsAPIException):
    pass


class ContactNotFoundError(WhatsAPIException):
    pass


class WhatsAPIDriver(object):
    """
    This is our main driver objects.

        .. note::

           Runs its own instance of selenium

        """
    _PROXY = None

    _URL = "https://web.whatsapp.com"

    _LOCAL_STORAGE_FILE = 'localStorage.json'

    _SELECTORS = {
        'firstrun': "#wrapper",
        'qrCode': "img[alt=\"Scan me!\"]",
        'qrCodePlain': "._2EZ_m",
        'mainPage': ".app.two",
        'chatList': ".infinite-list-viewport",
        'messageList': "#main > div > div:nth-child(1) > div > div.message-list",
        'unreadMessageBar': "#main > div > div:nth-child(1) > div > div.message-list > div.msg-unread",
        'searchBar': ".input",
        'searchCancel': ".icon-search-morph",
        'chats': ".infinite-list-item",
        'chatBar': 'div.input',
        'sendButton': 'button.icon:nth-child(3)',
        'LoadHistory': '.btn-more',
        'UnreadBadge': '.icon-meta',
        'UnreadChatBanner': '.message-list',
        'ReconnectLink': '.action',
        'WhatsappQrIcon': 'span.icon:nth-child(2)',
        'QRReloader': '.qr-wrapper-container'
    }

    _CLASSES = {
        'unreadBadge': 'icon-meta',
        'messageContent': "message-text",
        'messageList': "msg"
    }

    logger = logging.getLogger(__name__)
    driver = None

    # Profile points to the Firefox profile for firefox and Chrome cache for chrome
    # Do not alter this
    _profile = None

    def get_local_storage(self):
        local_storage = self.driver.execute_script('return window.localStorage;')
        escaped = {}
        for k,v in local_storage.items():
            escaped[k] = v.encode('unicode-escape').decode('ascii') if type(v) is str else v
        return escaped

    def set_local_storage(self, data):
        self.driver.execute_script(''.join(["window.localStorage.setItem('{}', '{}');".format(k, v)
                                            for k, v in data.items()]))

    def save_firefox_profile(self, remove_old=False):
        "Function to save the firefox profile to the permanant one"
        self.logger.info("Saving profile from %s to %s" % (self._profile.path, self._profile_path))

        if remove_old:
            tmp_path = "{}__tmp".format(self._profile_path)

            shutil.copytree(
                os.path.join(self._profile.path), tmp_path,
                ignore=shutil.ignore_patterns(
                    "parent.lock", "lock", ".parentlock"
                )
            )

            if os.path.exists(tmp_path) and len(os.listdir(tmp_path)) > 0:
                if os.path.exists(self._profile_path):
                    try:
                        shutil.rmtree(self._profile_path)
                    except OSError:
                        pass
                shutil.move(tmp_path, self._profile_path)
            else:
                shutil.rmtree(tmp_path)
                raise WhatsAPIException("Missing tmp firefox profile.")

        else:
            for item in os.listdir(self._profile.path):
                if item in ["parent.lock", "lock", ".parentlock"]:
                    continue
                s = os.path.join(self._profile.path, item)
                d = os.path.join(self._profile_path, item)
                if os.path.isdir(s):
                    shutil.copytree(s, d,
                                    ignore=shutil.ignore_patterns("parent.lock", "lock", ".parentlock"))
                else:
                    shutil.copy2(s, d)

        with open(os.path.join(self._profile_path, self._LOCAL_STORAGE_FILE), 'w') as f:
            f.write(dumps(self.get_local_storage()))

    def set_proxy(self, proxy):
        self.logger.info("Setting proxy to %s" % proxy)
        proxy_address, proxy_port = proxy.split(":")
        self._profile.set_preference("network.proxy.type", 1)
        self._profile.set_preference("network.proxy.http", proxy_address)
        self._profile.set_preference("network.proxy.http_port", int(proxy_port))
        self._profile.set_preference("network.proxy.ssl", proxy_address)
        self._profile.set_preference("network.proxy.ssl_port", int(proxy_port))

    def close(self):
        self.driver.close()

    def __init__(self, client="firefox", username="API", proxy=None, command_executor=None, loadstyles=False,
                 profile=None, headless=False, autoconnect=True, logger=None, extra_params=None):
        "Initialises the webdriver"

        self.logger = logger or self.logger
        extra_params = extra_params or {}

        if profile is not None:
            self._profile_path = profile
            self.logger.info("Checking for profile at %s" % self._profile_path)
            if not os.path.exists(self._profile_path):
                self.logger.critical("Could not find profile at %s" % profile)
                raise WhatsAPIException("Could not find profile at %s" % profile)
        else:
            self._profile_path = None

        self.client = client.lower()
        if self.client == "firefox":
            if self._profile_path is not None:
                self._profile = webdriver.FirefoxProfile(self._profile_path)
            else:
                self._profile = webdriver.FirefoxProfile()
            if not loadstyles:
                # Disable CSS
                self._profile.set_preference('permissions.default.stylesheet', 2)
                # Disable images
                self._profile.set_preference('permissions.default.image', 2)
                # Disable Flash
                self._profile.set_preference('dom.ipc.plugins.enabled.libflashplayer.so',
                                             'false')
            if proxy is not None:
                self.set_proxy(proxy)

            options = Options()

            if headless:
                options.set_headless()

            options.profile = self._profile

            capabilities = DesiredCapabilities.FIREFOX.copy()
            capabilities['webStorageEnabled'] = True

            self.logger.info("Starting webdriver")
            self.driver = webdriver.Firefox(capabilities=capabilities, options=options, **extra_params)

        elif self.client == "chrome":
            self._profile = webdriver.chrome.options.Options()
            if self._profile_path is not None:
                self._profile.add_argument("user-data-dir=%s" % self._profile_path)
            if proxy is not None:
                profile.add_argument('--proxy-server=%s' % proxy)
            self.driver = webdriver.Chrome(chrome_options=self._profile, **extra_params)

        elif client == 'remote':
            if self._profile_path is not None:
                self._profile = webdriver.FirefoxProfile(self._profile_path)
            else:
                self._profile = webdriver.FirefoxProfile()
            capabilities = DesiredCapabilities.FIREFOX.copy()
            self.driver = webdriver.Remote(
                command_executor=command_executor,
                desired_capabilities=capabilities,
                **extra_params
            )

        else:
            self.logger.error("Invalid client: %s" % client)
        self.username = username
        self.wapi_functions = WapiJsWrapper(self.driver)

        self.driver.set_script_timeout(500)
        self.driver.implicitly_wait(10)

        if autoconnect:
            self.connect()

    def connect(self):
        self.driver.get(self._URL)

        local_storage_file = os.path.join(self._profile.path, self._LOCAL_STORAGE_FILE)
        if os.path.exists(local_storage_file):
            with open(local_storage_file) as f:
                self.set_local_storage(loads(f.read()))

            self.driver.refresh()

    def get_browser_pid(self):
        """
        Method to get the pid of the browser instance.
        :return:
        """
        try:
            pid = self.driver.service.process.pid
        except Exception:
            pid = None
        return pid

    def is_logged_in(self):
        """Returns if user is logged. Can be used if non-block needed for wait_for_login"""
        # self.driver.find_element_by_css_selector(self._SELECTORS['mainPage'])
        # it becomes ridiculously slow if the element is not found.

        # instead we use this (temporary) solution:
        return self.wapi_functions.isLoggedIn()

    def wait_for_login(self, timeout=90):
        """Waits for the QR to go away"""
        WebDriverWait(self.driver, timeout).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, self._SELECTORS['mainPage']))
        )

    def get_qr_plain(self):
        return self.driver.find_element_by_css_selector(self._SELECTORS['qrCodePlain']).get_attribute("data-ref")

    def get_qr(self, filename=None):
        """Get pairing QR code from client"""
        if "Click to reload QR code" in self.driver.page_source:
            self.reload_qr()
        qr = self.driver.find_element_by_css_selector(self._SELECTORS['qrCode'])
        if filename is None:
            fd, fn_png = tempfile.mkstemp(prefix=self.username, suffix='.png')
        else:
            fd = os.open(filename, os.O_RDWR | os.O_CREAT)
            fn_png = os.path.abspath(filename)
        self.logger.debug("QRcode image saved at %s" % fn_png)
        qr.screenshot(fn_png)
        os.close(fd)
        return fn_png

    def screenshot(self, filename):
        self.driver.get_screenshot_as_file(filename)

    def get_contacts(self):
        """
        Fetches list of all contacts

        This will return chats with people from the address book only
        Use get_all_chats for all chats

        :return: List of contacts
        :rtype: list[Contact]
        """
        all_contacts = self.wapi_functions.getAllContacts()
        return [Contact(contact, self) for contact in all_contacts]

    def get_my_contacts(self):
        """
        Fetches list of added contacts

        :return: List of contacts
        :rtype: list[Contact]
        """
        my_contacts = self.wapi_functions.getMyContacts()
        return [Contact(contact, self) for contact in my_contacts]

    def get_all_chats(self):
        """
        Fetches all chats

        :return: List of chats
        :rtype: list[Chat]
        """
        return [factory_chat(chat, self) for chat in self.wapi_functions.getAllChats()]

    def get_all_chat_ids(self):
        """
        Fetches all chat ids

        :return: List of chat ids
        :rtype: list[str]
        """
        return self.wapi_functions.getAllChatIds()

    def get_unread(
            self, include_me=False, include_notifications=False,
            filter_week=True, specific_chat=None
    ):
        """
        Fetches unread messages

        :param include_me: Include user's messages
        :type include_me: bool or None
        :param include_notifications: Include events happening on chat
        :type include_notifications: bool or None
        :param filter_week: Filter only the last week of messages
        :type filter_week: bool
        :param specific_chat: Specific chat from where get messages.
        :type specific_chat: string
        :return: List of unread messages grouped by chats
        :rtype: list[MessageGroup]
        """

        seven_days_ago = int((datetime.now() - timedelta(days=7)).timestamp())
        if specific_chat is None:
            raw_message_groups = self.wapi_functions.getUnreadMessages(
                include_me, include_notifications
            )
        else:
            raw_message_groups = self.wapi_functions.getUnreadMessagesUsingChatId(
                specific_chat, include_me, include_notifications
            )

        unread_messages = []
        for raw_message_group in raw_message_groups:
            chat = factory_chat(raw_message_group, self)

            if filter_week:
                messages = sorted(
                    map(
                        lambda message: factory_message(message, self),
                        filter(
                            lambda msg: msg["timestamp"] >= seven_days_ago,
                            raw_message_group['messages']
                        )
                    ),
                    key=lambda message: message.timestamp
                )
            else:
                messages = list(
                    map(
                        lambda message: factory_message(message, self),
                        raw_message_group['messages']
                    )
                )

            unread_messages.append(MessageGroup(chat, messages))

        return unread_messages

    def get_all_messages_in_chat(self, chat, include_me=False, include_notifications=False):
        """
        Fetches messages in chat

        :param include_me: Include user's messages
        :type include_me: bool or None
        :param include_notifications: Include events happening on chat
        :type include_notifications: bool or None
        :return: List of messages in chat
        :rtype: list[Message]
        """
        message_objs = self.wapi_functions.getAllMessagesInChat(
            chat.get_id(), include_me, include_notifications
        )

        messages = []
        for message in message_objs:
            messages.append(factory_message(message, self))

        return messages

    def get_all_message_ids_in_chat(self, chat, include_me=False, include_notifications=False):
        """
        Fetches message ids in chat

        :param include_me: Include user's messages
        :type include_me: bool or None
        :param include_notifications: Include events happening on chat
        :type include_notifications: bool or None
        :return: List of message ids in chat
        :rtype: list[str]
        """
        return self.wapi_functions.getAllMessageIdsInChat(
            chat.get_id(), include_me, include_notifications
        )

    def get_message_by_id(self, message_id):
        """
        Fetch a message

        :return: Message or False
        :rtype: Message
        """
        result = self.wapi_functions.getMessageById(message_id)

        if result:
            result = factory_message(result, self)

        return result

    def get_contact_from_id(self, contact_id):
        contact = self.wapi_functions.getContact(contact_id)

        if contact is None:
            raise ContactNotFoundError("Contact {0} not found".format(contact_id))

        return Contact(contact, self)

    def get_chat_from_id(self, chat_id):
        chat = self.wapi_functions.getChatById(chat_id)
        if chat:
            return factory_chat(chat, self)

        raise ChatNotFoundError("Chat {0} not found".format(chat_id))

    def get_chat_from_phone_number(self, number):
        """
        Gets chat by phone number

        Number format should be as it appears in Whatsapp ID
        For example, for the number:
        +972-51-234-5678
        This function would receive:
        972512345678

        :param number: Phone number
        :return: Chat
        :rtype: Chat
        """
        for chat in self.get_all_chats():
            if not isinstance(chat, UserChat) or number not in chat.get_id():
                continue
            return chat

        raise ChatNotFoundError('Chat for phone {0} not found'.format(number))

    def reload_qr(self):
        self.driver.find_element_by_css_selector(self._SELECTORS['qrCode']).click()

    def get_status(self):
        if self.driver is None:
            return WhatsAPIDriverStatus.NotConnected
        if self.driver.session_id is None:
            return WhatsAPIDriverStatus.NotConnected
        try:
            self.driver.find_element_by_css_selector(self._SELECTORS['mainPage'])
            return WhatsAPIDriverStatus.LoggedIn
        except NoSuchElementException:
            pass
        try:
            self.driver.find_element_by_css_selector(self._SELECTORS['qrCode'])
            return WhatsAPIDriverStatus.NotLoggedIn
        except NoSuchElementException:
            pass
        return WhatsAPIDriverStatus.Unknown

    def contact_get_common_groups(self, contact_id):
        for group in self.wapi_functions.getCommonGroups(contact_id):
            yield factory_chat(group, self)

    def chat_exists(self, chat_id):
        result = self.wapi_functions.existsChatId(chat_id)
        return result

    def chat_send_message_to_new(self, chat_id, message):
        result = self.wapi_functions.sendMessageToID(chat_id, message)

        if not isinstance(result, bool):
            return factory_message(result, self)
        return result

    def chat_send_message(self, chat_id, message):
        result = self.wapi_functions.sendMessage(chat_id, message)

        if not isinstance(result, bool):
            return factory_message(result, self)
        return result

    def chat_send_message_async(self, chat_id, message):
        result = self.wapi_functions.sendMessageAsync(chat_id, message)
        return result

    def chat_send_seen(self, chat_id):
        return self.wapi_functions.sendSeen(chat_id)

    def chat_send_media(self, chat_id, media_base_64, filename, caption):
        result = self.wapi_functions.sendMedia(
            media_base_64, chat_id, filename, caption
        )
        return result

    def chat_send_media_async(
            self, chat_id, media_base_64, filename, caption, url_fallback
    ):
        result = self.wapi_functions.sendMediaAsync(
            media_base_64, chat_id, filename, caption, url_fallback
        )
        return result

    def chat_get_messages(self, chat_id, include_me=False, include_notifications=False):
        message_objs = self.wapi_functions.getAllMessagesInChat(chat_id, include_me, include_notifications)
        for message in message_objs:
            yield factory_message(message, self)

    def chat_load_earlier_messages(self, chat_id):
        self.wapi_functions.loadEarlierMessages(chat_id)

    def chat_load_all_earlier_messages(self, chat_id):
        self.wapi_functions.loadAllEarlierMessages(chat_id)

    def async_chat_load_all_earlier_messages(self, chat_id):
        self.wapi_functions.asyncLoadAllEarlierMessages(chat_id)

    def are_all_messages_loaded(self, chat_id):
        return self.wapi_functions.areAllMessagesLoaded(chat_id)

    def group_get_participants_ids(self, group_id):
        return self.wapi_functions.getGroupParticipantIDs(group_id)

    def group_get_participants(self, group_id):
        participant_ids = self.group_get_participants_ids(group_id)

        for participant_id in participant_ids:
            yield self.get_contact_from_id(participant_id)

    def group_get_admin_ids(self, group_id):
        return self.wapi_functions.getGroupAdmins(group_id)

    def group_get_admins(self, group_id):
        admin_ids = self.group_get_admin_ids(group_id)

        for admin_id in admin_ids:
            yield self.get_contact_from_id(admin_id)

    def download_file(self, url):
        return b64decode(self.wapi_functions.downloadFile(url))

    def download_media(self, media_msg, download_preview=False):
        try:
            if media_msg.content and download_preview:
                return BytesIO(b64decode(media_msg.content))
        except AttributeError:
            pass

        file_data = self.download_file(media_msg.client_url)

        media_key = b64decode(media_msg.media_key)
        derivative = HKDFv3().deriveSecrets(media_key,
                                            binascii.unhexlify(media_msg.crypt_keys[media_msg.type]),
                                            112)

        parts = ByteUtil.split(derivative, 16, 32)
        iv = parts[0]
        cipher_key = parts[1]
        e_file = file_data[:-10]

        cr_obj = Cipher(algorithms.AES(cipher_key), modes.CBC(iv), backend=default_backend())
        decryptor = cr_obj.decryptor()
        return BytesIO(decryptor.update(e_file) + decryptor.finalize())

    def mark_default_unread_messages(self):
        """
        Look for the latest unreplied messages received and mark them as unread.

        """
        self.wapi_functions.markDefaultUnreadMessages()

    def get_battery_level(self):
        """
        Check the battery level of device
        :return: int: Battery level
        """
        return self.wapi_functions.getBatteryLevel()

    def leave_group(self, chat_id):
        """
        Leave a group
        :param chat_id: id of group
        :return:
        """
        return self.wapi_functions.leaveGroup(chat_id)

    def delete_chat(self, chat_id):
        """
        Delete a chat
        :param chat_id: id of chat
        :return:
        """
        return self.wapi_functions.deleteConversation(chat_id)

    def get_all_messages_until_date(
            self, date=None, include_me=True, include_notifications=False
    ):
        """
        Get all the messages on whatsapp until a min(date, 7 days)
        :param date: Date until the messages are get.
        :type date: date in timestamp or None
        :param include_me: Include user's messages
        :type include_me: bool or None
        :param include_notifications: Include events happening on chat
        :type include_notifications: bool or None
        :return: List of messages grouped by chats
        :rtype: list[MessageGroup]
        """
        seven_days_ago = int((datetime.now() - timedelta(days=7)).timestamp())
        if date is None:
            date = seven_days_ago
        else:
            date = max(date, seven_days_ago)
        self.wapi_functions.loadEarlierMessagesTillDateAllChats(
            date
        )
        raw_message_groups = self.wapi_functions.getAllLatestMessages(
            include_me, include_notifications
        )

        unread_messages = []
        for raw_message_group in raw_message_groups:
            chat = factory_chat(raw_message_group, self)
            messages = sorted(
                map(
                    lambda message:  factory_message(message, self),
                    filter(
                        lambda msg: msg["timestamp"] >= date,
                        raw_message_group['messages']
                    )
                ),
                key=lambda message: message.timestamp
            )
            unread_messages.append(MessageGroup(chat, messages))

        return unread_messages

    def get_connection_status(self):
        """
        Get status of the browser instance.
        :return:
        """
        js_status = self.wapi_functions.getStatus()
        if js_status in ['CONNECTED']:
            status = 'RUNNING'
        elif js_status in ['API-ERROR']:
            status = 'API-ERROR'
        else:
            status = 'ERROR'
        return status

    def quit(self):
        self.driver.quit()
