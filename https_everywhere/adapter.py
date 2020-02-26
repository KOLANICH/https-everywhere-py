import logging

import urllib3
from urllib3.util.url import parse_url

import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.timeout import Timeout

from ._rules import https_url_rewrite, _get_rulesets
from ._chrome_preload_hsts import _preload_including_subdomains
from ._util import _check_in

from logzero import setup_logger

_REASON = "HTTPS Everywhere"
logger = setup_logger(name="httpseverwhere.adapter", level=logging.INFO)


def _generate_redirect(location, code=302):
    r = requests.Response()
    r.headers["Location"] = location
    r.encoding = "utf8"
    r.status_code = code
    r.reason = _REASON
    r._content = ""
    return r


class HTTPSEverywhereOnlyAdapter(HTTPAdapter):
    def __init__(self, *args, **kwargs):
        super(HTTPSEverywhereOnlyAdapter, self).__init__(*args, **kwargs)
        # prime cache
        _get_rulesets()

    def send(self, request, *args, **kwargs):
        url = request.url

        if url.startswith("http://"):
            url = https_url_rewrite(url)
            if url.startswith("https://"):
                logger.info("adapter redirecting {} to {}".format(request.url, url))
                response = _generate_redirect(url)
                response.request = request
                response.url = request.url
                return response

        return super(HTTPSEverywhereOnlyAdapter, self).send(request, *args, **kwargs)


class ChromePreloadHSTSAdapter(HTTPAdapter):
    def __init__(self, *args, **kwargs):
        super(ChromePreloadHSTSAdapter, self).__init__(*args, **kwargs)
        # prime cache
        self._domains = _preload_including_subdomains()

    def send(self, request, *args, **kwargs):
        url = request.url

        if url.startswith("http://"):
            p = parse_url(url)
            if _check_in(self._domains, p.host):
                new_url = "https:" + url[5:]
                logger.info("adapter redirecting {} to {}".format(request.url, new_url))
                response = _generate_redirect(new_url)
                response.request = request
                response.url = request.url
                return response

        return super(ChromePreloadHSTSAdapter, self).send(request, *args, **kwargs)


class HTTPSEverywhereAdapter(ChromePreloadHSTSAdapter, HTTPSEverywhereOnlyAdapter):
    pass


class RedirectAdapter(HTTPAdapter):  # pragma: no cover

    # TODO: add tests for this super class

    @staticmethod
    def gen_response(code=200, headers=None):
        r = requests.Response()
        r.encoding = "utf8"
        r.status_code = code
        r.reason = "FAKE REASON"
        r._content = ""

        if headers:
            r.headers.update(headers)
        return r

    def gen_redirect(self, location, code=302):
        response = self.gen_response(code, {"Location": location})
        return response

    def get_redirect(self, url):
        return

    def send(self, request, *args, **kwargs):
        code = 302
        rv = self.get_redirect(request.url)
        if rv is None:
            url = None
        elif isinstance(rv, requests.Response):
            logger.info(
                "adapter responding to {} with {}: {!r}".format(
                    request.url, rv.url, rv.headers
                )
            )
            rv.request = request
            rv.url = request.url
            return rv
        elif isinstance(rv, tuple):
            url, code = rv
        else:
            url = rv
        if url and url != request.url:
            # need to prevent redirecting to https when https has already downgraded to http

            response = self.gen_redirect(url, code)
            response.request = request
            response.url = request.url
            response._redirected = True
            logger.info("adapter redirecting {} to {}".format(request.url, url))
            return response

        try:
            logger.debug("no redirection of {} occurred".format(request.url))
            resp = super(RedirectAdapter, self).send(request, *args, **kwargs)
        except Exception as e:
            resp = self.handle_error(e, request)
        return resp

    def handle_error(self, exc, request=None):
        logger.error(
            "handle_error", exc.__class__.__module__, exc.__class__.__name__, exc
        )
        raise exc


class ForceHTTPSAdapter(HTTPAdapter):
    def __init__(self, *args, **kwargs):
        https_exclusions = kwargs.pop("https_exclusions", [])
        super(ForceHTTPSAdapter, self).__init__(*args, **kwargs)
        self._https_exclusions = https_exclusions

    def _prevent_https(self, tail):
        for rule in self._https_exclusions:
            if tail.startswith(rule):
                return True
        return False

    def send(self, request, *args, **kwargs):
        if request.url.startswith("https://"):
            tail = request.url[8:]
            if self._prevent_https(tail):
                logger.info("downgraded {} to http".format(request.url))
                response = _generate_redirect("http://" + tail)
                response.request = request
                response.url = request.url
                return response
        elif request.url.startswith("http://"):
            tail = request.url[7:]
            if not self._prevent_https(tail):
                logger.info("upgrading {} to https".format(request.url))
                response = _generate_redirect("https://" + tail)
                response.request = request
                response.url = request.url
                return response

        return super(ForceHTTPSAdapter, self).send(request, *args, **kwargs)


# Rename to CheckHTTPRedirectAdapter ?
# Allows for scenarios where https is broken, and http redirects to
# a different host, such as www.modwsgi.org
class PreferHTTPSAdapter(ForceHTTPSAdapter):

    _head_timeout = Timeout(connect=10, read=5)

    def _follow_redirects_on_http(self, url):
        previous_url = None
        while True:
            current_url = url
            # TODO: use same session
            # TODO: if http is a timeout, use a shorter timeout/retry for https
            #       as it is likely to also not work
            try:
                response = None
                try:
                    response = requests.head(
                        current_url, allow_redirects=False, timeout=self._head_timeout
                    )
                    response.raise_for_status()
                except Exception:  # pragma: no cover
                    # Add test case for this, possibly from code.google.com
                    if response and response.status_code == 403:
                        response = requests.get(
                            current_url,
                            allow_redirects=False,
                            timeout=self._head_timeout,
                        )
                        response.raise_for_status()
                    else:
                        raise
            except Exception as e:
                logger.info("head failed for {}: {!r}".format(current_url, e))
                return previous_url
            else:
                logger.debug(
                    "head",
                    current_url,
                    response.url,
                    response,
                    response.headers,
                    response.content,
                )
                location = response.headers.get("location")
                if not location or location == current_url:
                    return previous_url
                else:  # pragma: no cover
                    # modwsgi scenario
                    if location.startswith("https:"):
                        tail = location[8:]
                        if self._prevent_https(tail):
                            return "http://" + tail
                        return response
                    elif location.startswith("http://"):
                        previous_url = current_url
                        url = location
                    else:
                        raise RuntimeError(
                            "{} redirected to {}".format(current_url, location)
                        )

    def send(self, request, *args, **kwargs):
        url = request.url
        if url.startswith("https://"):
            tail = url[8:]
            if self._prevent_https(tail):
                logger.info("downgraded {} to http".format(url))
                response = _generate_redirect("http://" + tail)
                response.request = request
                response.url = url
                return response
        elif url.startswith("http://"):
            tail = url[7:]
            if not self._prevent_https(tail):
                logger.debug("checking {} for redirects".format(url))
                redirect = self._follow_redirects_on_http(url)
                if redirect:  # pragma: no cover
                    if not isinstance(redirect, str):
                        # Following redirects may provide a redirect response object
                        # This was the modwsgi scenario
                        logger.info(
                            "upgrading {} to https with {}".format(url, redirect.url)
                        )
                        return redirect
                    elif redirect != url:
                        if redirect.startswith("http://"):
                            tail = url[7:]
                        else:
                            raise RuntimeError(
                                "Unexpectedly {} redirected to {}".format(url, redirect)
                            )
                logger.info("upgrading {} to https".format(url))

                response = _generate_redirect("https://" + tail)
                response.request = request
                response.url = request.url
                return response

        return super(PreferHTTPSAdapter, self).send(request, *args, **kwargs)


class UpgradeHTTPSAdapter(ForceHTTPSAdapter):

    _https_downgrade_exceptions = (
        requests.exceptions.ConnectionError,
        urllib3.exceptions.MaxRetryError,
        requests.exceptions.SSLError,
    )

    def send(self, request, *args, **kwargs):
        url = request.url

        if not url.startswith("https://"):
            response = super(UpgradeHTTPSAdapter, self).send(request, *args, **kwargs)
            logger.debug("http response reason: {}".format(response.reason))
            return response

        try:
            return super(UpgradeHTTPSAdapter, self).send(request, *args, **kwargs)
        except self._https_downgrade_exceptions as e:
            logger.info("downgrading {} to http due to {}".format(url, e))

        request.url = "http://" + url[8:]
        # Note: skipping base classes including ForceHTTPSAdapter
        return super(ForceHTTPSAdapter, self).send(request, *args, **kwargs)


class SafeUpgradeHTTPSAdapter(ForceHTTPSAdapter):

    _https_downgrade_exceptions = (
        requests.exceptions.ConnectionError,
        urllib3.exceptions.MaxRetryError,
        requests.exceptions.SSLError,
    )

    def send(self, request, *args, **kwargs):
        url = request.url

        if not url.startswith("https://"):
            response = super(SafeUpgradeHTTPSAdapter, self).send(
                request, *args, **kwargs
            )
            logger.debug("http response reason: {}".format(response.reason))
            if response.reason != _REASON:  # pragma: no cover
                return response
            request.url = response.headers["location"]

        try:
            response = super(SafeUpgradeHTTPSAdapter, self).send(
                request, *args, **kwargs
            )
            redirect = response.headers.get("location")
            if not redirect or redirect != url:
                return response
        except self._https_downgrade_exceptions as e:
            logger.info("downgrading {} to http due to {}".format(url, e))

        request.url = "http://" + request.url[8:]
        # Note: skipping base classes including ForceHTTPSAdapter
        return super(ForceHTTPSAdapter, self).send(request, *args, **kwargs)