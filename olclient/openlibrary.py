#-*- encoding: utf-8 -*-

"""Basic wrapper (client) over OpenLibrary REST API"""

from __future__ import absolute_import, division, print_function

from collections import namedtuple
import datetime
import json
import logging
import re
import urllib, urllib2

import backoff
import requests

from .book import Book, Author
from .config import Config
from .utils import parse_datetime


logger = logging.getLogger('openlibrary')


class OpenLibrary(object):

    """Open Library API Client.

    Usage:
        >>> ol = OpenLibrary(base_url="http://0.0.0.0:8080")
        ... #  Create a new book
        ... book = ol.create_book(Book(
        ...     title=u"Wie die Weißen Engel die Blauen Tiger zur Schnecke machten",
        ...     author=Author(name=u"Walter Kort"), publisher=u"Bertelsmann",
        ...     isbn=u"3570028364", publish_date=u"1982"))

        >>> ol = OpenLibrary("http://0.0.0.0:8080")
        ... #  Fetch and update an existing book
        ... book = ol.get_book_by_isbn(u"3570028364")
        ... book.title = u"Wie die Weißen Engel die Blauen Tiger zur Schnecke machten"
        ... book.save(comment="correcting title")
    """

    VALID_IDS = ['isbn_10', 'isbn_13', 'lccn']
    BACKOFF_KWARGS = {
        'wait_gen': backoff.expo,
        'exception': requests.exceptions.RequestException,
        'max_tries': 5
    }
    
    def __init__(self, base_url=None, credentials=None):
        ol_config = Config().get_config()['openlibrary']
        default_base_url = ol_config['url'].rstrip('/')
        self.base_url = base_url or default_base_url
        self.session = requests.Session()
        credentials = credentials or ol_config.get('credentials')
        if credentials:
            self.username = credentials.username
            self.login(credentials)

    def login(self, credentials):
        """Login to Open Library with given credentials"""
        err = lambda e: logger.exception("Error at login: %s", e)
        headers = {'Content-Type': 'application/json'}
        url = self.base_url + '/account/login'
        data = json.dumps(credentials._asdict())

        @backoff.on_exception(on_giveup=err, **self.BACKOFF_KWARGS)
        def _login(url, headers, data):
            return self.session.post(url, data=data, headers=headers)

        response = _login(url, headers, data)
        if 'Set-Cookie' not in response.headers:
            raise Exception("No cookie set")

    def get_matching_authors_by_name(self, name, limit=1):
        """Finds a list of OpenLibrary authors with similar names to the
        search query using the Author auto-complete API.

        Args:
            name (unicode) - name of author to search for within OpenLibrary's
                             database of authors

        Returns:
            A (list) of matching authors from the OpenLibrary
            authors autocomplete API
        """
        if name:
            err = lambda e: logger.exception("Error fetching author matches: %s", e)
            url = self.base_url + '/authors/_autocomplete?q=%s&limit=%s' \
                  % (name, limit)

            @backoff.on_exception(on_giveup=err, **self.BACKOFF_KWARGS)
            def _get_matching_authors_by_name(url):
                return self.session.get(url)

            response = _get_matching_authors_by_name(url)
            author_matches = response.json()
            return author_matches
        return []

    def get_matching_authors_olid(self, name):
        """Uses the Authors auto-complete API to find OpenLibrary Authors with
        similar names. If any name is an exact match and there's only
        one exact match (e.g. not a common name like "Mike Smith"
        which may have multiple valid results) then return the
        matching author's 'key' (i.e. olid). Otherwise, return None

        Args:
            name (unicode) - name of an Author to search for within OpenLibrary

        Returns:
            olid (unicode)
        """
        authors = self.get_matching_authors_by_name(name)
        _name = name.lower().strip()
        for author in authors:
            if _name == author['name'].lower().strip():
                return author['key'].split('/')[-1]
        return None


    def create_book(self, book, debug=False):
        """Create a new OpenLibrary Book using the /books/add endpoint

        Args:
           book (Book)

        Usage:
            >>> ol = OpenLibrary()
            ... book = ol.create_book(Book(
            ...     title=u"Wie die Weißen Engel die Blauen Tiger zur Schnecke machten",
            ...     author=Author(name=u"Walter Kort"), publisher=u"Bertelsmann",
            ...     isbn=u"3570028364", publish_date=u"1982"))
        """
        def get_primary_identifier():
            id_name, id_value = None, None
            for valid_key in self.VALID_IDS:
                if valid_key in book.identifiers:
                    id_name = valid_key
                    id_value = book.identifiers[valid_key][0]
                    break

            if not (id_name and id_value):
                raise ValueError("ISBN10/13 or LCCN required")
            return id_name, id_value

        id_name, id_value = get_primary_identifier()
        primary_author = book.primary_author
        author_name = primary_author.name if primary_author else u""
        author_olid = self.get_matching_authors_olid(author_name)
        author_key = ('/authors/' + author_olid) if author_olid else  u'__new__'
        return self._create_book(
            title=book.title,
            author_name=author_name,
            author_key=author_key,
            publish_date=book.publish_date,
            publisher=book.publisher,
            id_name=id_name,
            id_value=id_value,
            debug=debug)

    def _create_book(self, title, author_name, author_key,
                    publish_date, publisher, id_name, id_value,
                    debug=False):

        if id_name not in self.VALID_IDS:
            raise ValueError("Invalid `id_name`. Must be one of %s, got %s" \
                             % (self.VALID_IDS, id_name))

        err = lambda e: logger.exception("Error creating OpenLibrary book: %s", e)
        url = self.base_url + '/books/add'
        data = {
            "title": title,
            "author_name": author_name,
            "author_key": author_key,
            "publish_date": publish_date,
            "publisher": publisher,
            "id_name": id_name,
            "id_value": id_value,
            "_save": ""
        }
        if debug:
            return data

        @backoff.on_exception(on_giveup=err, **self.BACKOFF_KWARGS)
        def _create_book_post(url, data=data):
            return self.session.post(url, data=data)

        response = _create_book_post(url, data=data)
        return self._extract_olid_from_url(response.url, url_type="books")

    def get_book_by_olid(self, olid):
        """Retrieves a single book from OpenLibrary as json and marshals it into
        an olclient Book.

        Warnings:
            Currently, the marshaling is not complete. While it
            generates/returns a valid book, ideally we want the
            OpenLibrary fields to be converted into a format which is
            consistent with how we are using olclient Book to create
            OpenLibrary books -- i.e. authors = Author objects,
            publishers list instead of publisher, identifiers (instead
            of key and isbn). The goal is to enable service to
            interoperate with the Book object and for OpenLibrary to
            be able to marshal the book object into a form it can use
            (or marshal its internal book json into a form others can
            use).

        Usage:
            >>> from olclient import OpenLibrary
            >>> ol = OpenLibrary()
            >>> ol.get_book_by_olid('OL25944230M')
            <class 'olclient.book.Book' {'publisher': None, 'subtitle': '', 'last_modified': {u'type': u'/type/datetime', u'value': u'2016-09-07T00:31:28.769832'}, 'title': u'Analogschaltungen der Me und Regeltechnik', 'publishers': [u'Vogel-Verl.'], 'identifiers': {}, 'cover': '', 'created': {u'type': u'/type/datetime', u'value': u'2016-09-07T00:31:28.769832'}, 'isbn_10': [u'3802306813'], 'publish_date': 1982, 'key': u'/books/OL25944230M', 'authors': [], 'latest_revision': 1, 'works': [{u'key': u'/works/OL17365510W'}], 'type': {u'key': u'/type/edition'}, 'pages': None, 'revision': 1}>
        """
        err = lambda e: logger.exception("Error retrieving OpenLibrary " \
                                         "book: %s", e)
        url = self.base_url + '/books/%s.json' % olid

        @backoff.on_exception(on_giveup=err, **self.BACKOFF_KWARGS)
        def _get_book_by_olid(url):
            return self.session.get(url)

        response = _get_book_by_olid(url)
        # XXX need a way to convert OL book json -> book (and back)
        return Book(**response.json())

    def get_book_by_metadata(self, title, author=None):
        """Get the *closest* matching result in OpenLibrary based on a title
        and author.

        Args:
            title (unicode)
            author (unicode)

        Returns:
            (book.Book)

        Usage:
            >>> ol = OpenLibrary()
            ... ol.get_book_by_metadata(
            ...     title=u'The Autobiography of Benjamin Franklin')
        """
        err = lambda e: logger.exception("Error retrieving metadata " \
                                         "for book: %s", e)
        url = '%s/search.json?title=%s' % (self.base_url, title)
        if author:
            url += '&author=%s' % author

        @backoff.on_exception(on_giveup=err, **self.BACKOFF_KWARGS)
        def _get_book_by_metadata(url):
            return requests.get(url)

        response = _get_book_by_metadata(url)

        try:
            results = Results(**response.json())
        except ValueError as e:
            logger.exception(e)
            return None

        if results.num_found:
            return results.first.to_book()

        return None

    def get_book_by_isbn(self, isbn):
        """Marshals the output OpenLibrary Book json API
        into (Book) format

        Args:
            isbn (unicode)

        Returns:
            (Book) from the books API endpoint for an item if it
            exists (see fields at
            https://openlibrary.org/dev/docs/api/books) or None if
            there is no match or if the json is malformed.

        Usage:
        """
        err = lambda e: logger.exception("Error retrieving OpenLibrary " \
                                         "book by isbn: %s", e)
        url = self.base_url + '/api/books?bibkeys=ISBN:' + isbn + \
              '&format=json&jscmd=data'

        @backoff.on_exception(on_giveup=err, **self.BACKOFF_KWARGS)
        def _get_book_by_isbn(url):
            return requests.get(url)

        response = _get_book_by_isbn(url)

        try:
            result = response.json()
        except ValueError as e:
            logger.exception(e)
            return None

        isbn_key = u'ISBN:%s' % isbn
        if isbn_key in result:
            edition = result[isbn_key]
            print(edition['key'])
            edition['identifiers'][u'olid'] = [self._extract_olid_from_url(
                edition.pop('key'), url_type="books")]
            authors = edition.pop('authors', [])
            edition['authors'] = [
                Author(name=author['name'], olid=self._extract_olid_from_url(
                    author['url'], url_type="authors"))
                for author in authors]
            return Book(**edition)

        return None

    def get_olid_by_isbn(self, isbn):
        """Looks up a ISBN10/13 in OpenLibrary and returns a matching olid (by
        default) or metadata (if metadata=True specified) if a match exists.

        Args:
            isbn (unicode)

        Returns:
            olid (unicode) or None

        Usage:
            >>> ol = OpenLibrary()
            ... ol.get_book_by_isbn(u'9780747550303')
            u'OL1429049M'
        """
        err = lambda e: logger.exception("Error retrieving OpenLibrary " \
                                         "ID by isbn: %s", e)
        url = self.base_url + '/api/books?bibkeys=ISBN:' + isbn + '&format=json'

        @backoff.on_exception(on_giveup=err, **self.BACKOFF_KWARGS)
        def _get_olid_by_isbn(url):
            """Makes best effort to perform request w/ exponential backoff"""
            return requests.get(url)

        # Let the exception be handled up the stack
        response = _get_olid_by_isbn(url)

        try:
            results = response.json()
        except ValueError as e:
            logger.exception(e)
            return None
        isbn_key = u'ISBN:%s' % isbn
        if isbn_key in results:
            book_url = results[isbn_key].get('info_url', '')
            return self._extract_olid_from_url(book_url, url_type="books")
        return None

    @staticmethod
    def _extract_olid_from_url(url, url_type):
        """No single field has the match's OpenLibrary ID in isolation so we
        extract it from the info_url field.

        Args:
            url_type (unicode) - "books", "authors", "works", etc
                                 which are found in the ol url, e.g.:
                                 openlibrary.org/books/...

        Returns:
            olid (unicode)

        Usage:
            >>> url = u'https://openlibrary.org/books/OL25943366M'
            >>> _extract_olid_from_url(url, u"books")
                u"OL25943366M"
        """
        ol_url_pattern = r'[/]%s[/]([0-9a-zA-Z]+)' % url_type
        try:
            return re.search(ol_url_pattern, url).group(1)
        except AttributeError:
            return None  # No match


class Results(object):

    """Container for the results of the Search API"""

    def __init__(self, start=0, num_found=0, docs=None, **kwargs):
        self.start = start
        self.num_found = num_found
        self.docs = [self.Document(**doc) for doc in docs] or []

    @property
    def first(self):
        if self.docs:
            return self.docs[0]


    class Document(object):
        """An aggregate OpenLibrary Work summarizing all Editions of a Book"""

        def __init__(self, key, title=u"", subtitle=None, subject=None,
                     author_name=u"", author_key=None, edition_key=None,
                     language="", publisher=None, publish_date=None,
                     publish_place=None, first_publish_year=None,
                     isbns=None, lccn=None, oclc=None, id_goodreads=None,
                     id_librarything=None, **kwargs):
            """
            Args:
                key (unicode) - a '/<type>/<OLID>' uri, e.g. '/works/OLXXXXXX'
                title (unicode)
                subtitle (unicode) [optional]
                subject (list of unicode) [optional]
                author_name (list of unicode)
                author_key (list of unicode) - list of author OLIDs
                edition_key (list of unicode) - list of edition OLIDs
                language (unicode)
                publisher (list of unicode)
                publish_date (list unicode)
                publish_place (list unicode)
                first_publish_year (int)
                isbns (list unicode)
                lccn (list unicode)
                oclc (list unicode)
                id_goodreads (list unicode)
                id_librarything (list unicode)
            """
            work_olid = OpenLibrary._extract_olid_from_url(key, "works")
            edition_olids = edition_key

            self.title = title
            self.subtitle = subtitle
            self.subjects = subject
            # XXX test that during the zip, author_name and author_key
            # correspond to each other one-to-one, in order
            self.authors = [Author(name=name, olid=author_olid)
                            for (name, author_olid) in
                            zip(author_name or [], author_key or [])]
            self.publishers = publisher
            self.publish_dates = publish_date
            self.publish_places = publish_place
            self.first_publish_year = first_publish_year
            self.edition_olids = edition_olids
            self.language = language

            # These keys all map to [lists] of (usually one) unicode ids
            self.identifiers = {
                'olid': [work_olid],
                'isbns': isbns or [],
                'oclc': oclc or [],
                'lccn': lccn or [],
                'goodreads': id_goodreads or [],
                'librarything': id_librarything or []
            }

        def to_book(self):
            publisher = self.publishers[0] if self.publishers else ""
            return Book(title=self.title, subtitle=self.subtitle,
                        identifiers=self.identifiers,
                        authors=self.authors, publisher=publisher,
                        publish_date=self.first_publish_year)