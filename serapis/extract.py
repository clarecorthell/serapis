#!/usr/bin/env python
# coding=utf-8
"""
Extract module

"""
from __future__ import unicode_literals
from __future__ import absolute_import

__author__ = "Clare Corthell"
__copyright__ = "Copyright 2015, summer.ai"
__date__ = "2015-11-20"
__email__ = "clare@summer.ai"

import requests
from .config import config  # Make sure to use absolute imports here
import html2text
from bs4 import BeautifulSoup
from serapis.util import squashed
from serapis.language import is_english
from serapis.preprocess import paragraph_to_sentences, qualify_sentence, clean_sentence
from serapis.util import get_source_from_url
import re
import time
import logging


log = logging.getLogger('serapis.extract')
html_parser = html2text.HTML2Text()
for option in ('ignore_links', 'ignore_images', 'ignore_emphasis', 'bypass_tables', 'unicode_snob', 'ignore_anchors'):
    setattr(html_parser, option, True)
html_parser.body_width = 0


class PageRequest(object):
    """
    Requests and parses a single page.

    Properties:
        structured: dict -- Contains term, url, doc, features, variants,
                    sentences, author, and title of the page
    Methods:
        request_page -- Loads the page (automatically called by __init__)
        parse_response -- parses the page (automatically called by __init__)
        extract_sentences -- sets self.sentences and self.variants
        get_meta -- sets self.title and self.author from html
        get_html_features -- sets self.features from html
    """

    OPENING_QUOTES = ("\"", "'", "&quot;", "“", "&ldquo;", "‘", "&lsquo;", "«", "&laquo;", "‹", "&lsaquo;", "„", "&bdquo;", "‚", "&sbquo;")
    CLOSING_QUOTES = ("'", "&quot;", "”", "&rdquo;", "’", "&rsquo;", "»", "&raquo;", "›", "&rsaquo;", "“", "&ldquo;", "‘", "&lsquo;")

    def request_page(self):
        attempts = config.request_retry
        while attempts:
            try:
                response = requests.get(self.url, timeout=10)
            except:
                response = None
            if response and hasattr(response, "text"):
                self.response = response
                return self.response
            else:
                # log.warning("Didn't get  url: %s" % self.url)
                attempts -= 1
                time.sleep(config.request_seconds_before_retry)
        
        log.error("Failed to return page for url: %s" % self.url)
        return None

    def extract_sentences(self, page_text):
        """Finds all sentences that contain the term or a spelling variants.
        Sets self.sentences ans self.variants.

        Args:
            page_text: str
        Returns
            str -- cleaned page text.
        """
        doc = []
        for paragraph in page_text.split('\n\n'):
            if is_english(paragraph):
                for sentence in paragraph_to_sentences(paragraph, self.term):
                    if qualify_sentence(sentence):
                        doc.append(sentence)
                        s_clean, variants = clean_sentence(sentence, self.term)
                        if variants and s_clean not in [s['s_clean'] for s in self.sentences]:
                            self.variants.update(variants)
                            self.sentences.append({
                                's': sentence,
                                's_clean': s_clean
                            })
        return " ".join(doc)

    def get_meta(self, page_html):
        """
        Scan meta tags of given html title and author.
        """
        # Common ways to encode authorship information
        prop_names = {
            "name": "author",  # W3C
            "property": "og:author",  # Facebook OG
            "property": "article:author",  # Pinterest and Twitter meta
        }
        authors = []

        for prop, value in prop_names.items():
            m_re = r"< *meta.+{}=.?{}.? content=(\"[^\"]+\"|'[^']+') [^>]>".format(prop, value)
            for m in re.findall(m_re, page_html):
                authors.extend([m.strip("'\"")])

        # Pick the first author that looks reasonable
        authors = filter(lambda author: author and not author.startswith("http"), authors)
        self.author = authors[0] if authors else None

        title_tags = re.findall("< *title *>([^<]+)</ *title *>", page_html)
        self.title = title_tags[0].strip(" \n") if title_tags else None

    def get_html_features(self, html):
        """Detects whether the search term exists is highlighted (bolded, emphasised) or
        in quotes. Needs to be called after self.request_page.
        """
        if not self.term:
            return None

        minimal_html = squashed(html, keep='<>/&;')
        minimal_term = squashed(self.term)

        highlight_re = r"<(em|i|b|strong|span)[^>]*> *{}[ ,:]*</\1>".format(minimal_term)
        quote_re = r"<({})[^>]*> *{}[ ,:]*</({})>".format("|".join(self.OPENING_QUOTES), minimal_term, "|".join(self.CLOSING_QUOTES))

        self.features = {
            "highlighted": bool(re.search(highlight_re, minimal_html, re.IGNORECASE)),
            "quotes": bool(re.search(quote_re, minimal_html, re.IGNORECASE)),
        }
        
    def parse_response(self):
        """
        Returns elements extracted from html

        """
        self.html = self.response.text

        # Try Aaron's html2text first, and fall back on beautiful soup
        try:
            raw = html_parser.handle(self.html)
        except:
            log.info("Falling back on BeautifulSoup for '{}'".format(self.url))
            soup = BeautifulSoup(self.html, 'html5lib')
            for el in soup(['head', 'script']) + soup(class_='comments') + soup(id="comments"):
                el.extract()
            raw = re.sub(r"\n+", "\n\n", soup.get_text())
        self.text = self.extract_sentences(raw)
        self.get_html_features(self.html)
        self.get_meta(self.html)

    @property
    def structured(self):
        structure = {
            "term": self.term,
            "url": self.url,
            "source": get_source_from_url(self.url),
            "doc": self.text,
            "features": self.features,
            "variants": list(self.variants),  # Sets are not JSON serializable
            "sentences": self.sentences,
            "author": self.author,
            "title": self.title
        }

        if config.save_html:
            structure["html"] = self.html
        return structure

    def __init__(self, url, term, run=True):
        """
        Args:
            url: str
            term: str
            run: bool -- If False, does not automatically request page
        """
        self.url = url
        self.term = term
        self.variants = set()
        self.sentences = []
        self.text = ""
        self.html = ""
        self.features = {}
        self.author = None
        self.title = None

        if run:
            self.response = self.request_page()
            if self.response:
                self.parse_response()


class DiffbotRequest(PageRequest):
    """
    Uses Diffbot to parse a page. Same interface as PageRequest.
    """

    diff_article_api = 'http://api.diffbot.com/v3/article'

    def request_page(self):
        params = {
            'token': config.credentials.diffbot,
            'url': self.url,
            'mode': 'article',
            'discussion': False
        }

        try:
            self.response = requests.get(self.diff_article_api, params=params).json()
            assert 'objects' in self.response
            assert self.response['objects']
        except:
            log.error("Failed to return page for url: %s" % self.url)
            return None

        return self.response

    def parse_response(self):
        obj = self.response['objects'][0]
        self.text = obj.get('text')
        self.html = obj.get('html')
        self.author = obj.get('author')
        self.title = obj.get('title')

        self.extract_sentences(self.html)
        self.get_html_features(self.html)
