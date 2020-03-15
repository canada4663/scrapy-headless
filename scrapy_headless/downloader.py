import threading

from scrapy.utils.python import to_bytes
from scrapy.core.downloader.handlers.http11 import HTTP11DownloadHandler
from scrapy.exceptions import NotConfigured
from scrapy.http import HtmlResponse
from selenium.webdriver.common.proxy import Proxy, ProxyType
from selenium.common.exceptions import WebDriverException
from selenium.webdriver import Remote
from twisted.python.threadpool import ThreadPool
from twisted.internet import threads, reactor
from twisted.web.client import ResponseFailed
import time

from scrapy_headless.request import HeadlessRequest

CHANGE_PROXY = """
var prefs =
Components.classes["@mozilla.org/preferences-service;1"].getService(Components.interfaces.nsIPrefBranch);
prefs.setIntPref('network.proxy.type', 1);
prefs.setCharPref("network.proxy.http", "{proxy}");
prefs.setIntPref("network.proxy.http_port", {proxy_port});
prefs.setCharPref("network.proxy.ssl", "{proxy}");
prefs.setIntPref("network.proxy.ssl_port", {proxy_port});
"""

class HeadlessDownloadHandler(object):
    lazy = False
    _default_handler_cls = HTTP11DownloadHandler

    def __init__(self, settings):
        if "SELENIUM_GRID_URL" not in settings:
            raise NotConfigured("SELENIUM_GRID_URL has to be set")
        if "SELENIUM_NODES" not in settings:
            raise NotConfigured("SELENIUM_NODES has to be set")
        if "SELENIUM_CAPABILITIES" not in settings:
            raise NotConfigured("SELENIUM_CAPABILITIES has to be set")
        self.grid_url = settings["SELENIUM_GRID_URL"]
        self.selenium_nodes = settings["SELENIUM_NODES"]
        self.capabilities = settings["SELENIUM_CAPABILITIES"]
        selenium_proxy = settings.get("SELENIUM_PROXY", None)
        if selenium_proxy:
            self.set_selenium_proxy(selenium_proxy)
        self._drivers = set()
        self._data = threading.local()
        self._threadpool = ThreadPool(self.selenium_nodes, self.selenium_nodes)
        self._default_handler = self._default_handler_cls(settings)
	
    @classmethod
    def from_crawler(cls, crawler):
        downloader = cls(crawler.settings)
        crawler.signals.connect(downloader.spider_closed, signals.spider_closed)
        return downloader

    def spider_closed(self):
        print("CLOSING HEADLESS DOWNLOADER")
        for driver in self._drivers:
            driver.quit()
        self._threadpool.stop()

    def set_selenium_proxy(self, selenium_proxy):
        proxy = Proxy()
        proxy.http_proxy = selenium_proxy
        proxy.ftp_proxy = selenium_proxy
        proxy.sslProxy = selenium_proxy
        proxy.no_proxy = None
        proxy.proxy_type = ProxyType.MANUAL
        proxy.add_to_capabilities(self.capabilities)
        self.capabilities["acceptSslCerts"] = True

    def change_selenium_proxy(self, driver, spider, proxy, proxy_port):
        driver.get('about:config')
        script = CHANGE_PROXY.format(proxy=proxy, proxy_port=proxy_port)
        driver.execute_script(script)
        driver.delete_all_cookies()

    def download_request(self, request, spider):
        if isinstance(request, HeadlessRequest):
            if not self._threadpool.started:
                self._threadpool.start()
            return threads.deferToThreadPool(
                reactor, self._threadpool, self.process_request, request, spider
            )
        return self._default_handler.download_request(request, spider)

    def process_request(self, request, spider):
        spider.logger.debug("HEADLESS REQUEST: %s" % request.url)
        driver = self.get_driver(spider)

        if request.meta.get('proxy', False):
            if 'http://' in request.meta['proxy']:
                request.meta['proxy'] = request.meta['proxy'].replace('http://','')
            proxy = request.meta['proxy'].split(':')[0]
            proxy_port = request.meta['proxy'].split(':')[1]
            self.change_selenium_proxy(driver, spider, proxy, proxy_port)
            #driver.get("view-source:https://api.ipify.org?format=json")
            #spider.logger.debug("PROXY IP: %s",
            #                      driver.find_element_by_css_selector('pre').text)
        else:
            spider.logger.warning("HEADLESS: NO PROXY: %s", request.meta)

        try:
            driver.get(request.url)
            spider.logger.debug("HEADLESS REQUEST: INITIAL GET: %s" % driver.title)
            if request.driver_callback is not None:
                return request.driver_callback(driver, request, spider)

            body = to_bytes(driver.page_source)
            curr_url = driver.current_url

        except WebDriverException as e:
            spider.logger.warning("WebDriverException %s" % e)
            return HtmlResponse(request.url, status=400, request=request)

        return HtmlResponse(curr_url, body=body, encoding="utf-8", request=request)

    def get_driver(self, spider): 
        try:
            driver = self._data.driver
        except AttributeError:
            driver = Remote(
                command_executor=self.grid_url, desired_capabilities=self.capabilities
            )
            driver.set_page_load_timeout(180)
            #driver.maximize_window()
            self._drivers.add(driver)
            self._data.driver = driver
        spider.logger.debug("HEADLESS REQUEST: GET DRIVER: %s" % driver)
        return driver
