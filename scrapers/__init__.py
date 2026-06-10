from scrapers.estate_ethiopia import EstateEthiopiaScraper
from scrapers.ethiopia_property import EthiopiaPropertyScraper
from scrapers.megbex import MegbexScraper
from scrapers.realtors_ethiopia import RealtorsEthiopiaScraper
from scrapers.zemen_broker import ZemenBrokerScraper
from scrapers.qemer import QemerScraper
from scrapers.google_search import GoogleSearchScraper
from scrapers.beteseb import BetesebScraper

ALL_SCRAPERS = [
    GoogleSearchScraper,
    EstateEthiopiaScraper,
    EthiopiaPropertyScraper,
    MegbexScraper,
    RealtorsEthiopiaScraper,
    ZemenBrokerScraper,
    QemerScraper,
    BetesebScraper,
]

__all__ = ["ALL_SCRAPERS"]
