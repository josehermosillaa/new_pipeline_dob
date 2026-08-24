import json
import logging
import os
import time


DOBNOW_URL = "https://a810-dobnow.nyc.gov/Publish/"
PUBLIC_PATH = "/Publish/WrapperPP/PublicPortal.svc"
SERVICE_PATH = "/Publish/WrapperServicePP/WrapperService.svc"

BOROUGH_MAP = {
    "Manhattan": "MANHATTAN",
    "Bronx": "BRONX",
    "Brooklyn": "BROOKLYN",
    "Queens": "QUEENS",
    "Staten Island": "STATEN ISLAND",
}


class BlockedError(RuntimeError):
    pass


class RequestError(RuntimeError):
    pass


class DOBNowClient:
    """Cliente autónomo que ejecuta las solicitudes dentro del navegador autenticado."""

    def __init__(self, profile, cdp_port=0, logger=None):
        self.profile = os.path.abspath(profile)
        self.cdp_port = cdp_port
        self.log = logger or logging.getLogger(__name__)
        self.pw = None
        self.context = None
        self.page = None

    def open(self):
        try:
            from patchright.sync_api import sync_playwright
        except ImportError as exc:
            raise RequestError("Patchright no esta instalado") from exc
        self.pw = sync_playwright().start()
        if self.cdp_port:
            browser = self.pw.chromium.connect_over_cdp(f"http://127.0.0.1:{self.cdp_port}")
            if not browser.contexts:
                raise RequestError("Chrome CDP no tiene contextos disponibles")
            self.context = browser.contexts[0]
            self.page = next(
                (page for page in self.context.pages if "dobnow" in (page.url or "").lower()),
                None,
            )
            if self.page is None:
                self.page = self.context.new_page()
                self.page.goto(DOBNOW_URL, wait_until="domcontentloaded", timeout=60000)
        else:
            os.makedirs(self.profile, exist_ok=True)
            self.context = self.pw.chromium.launch_persistent_context(
                user_data_dir=self.profile,
                channel="chrome",
                headless=False,
                no_viewport=True,
                args=["--no-first-run", "--no-default-browser-check"],
            )
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.goto(DOBNOW_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)
        self.assert_healthy(require_angular=True)
        self.log.info("Sesion DOB NOW disponible; modo=%s", "CDP" if self.cdp_port else "standalone")
        return self

    def close(self):
        try:
            if self.context is not None and not self.cdp_port:
                self.context.close()
        except Exception as exc:
            self.log.warning("No se pudo cerrar el contexto Chrome: %s", exc)
        try:
            if self.pw is not None:
                self.pw.stop()
        except Exception as exc:
            self.log.warning("No se pudo detener Patchright: %s", exc)
        self.pw = self.context = self.page = None

    def _page_access_denied(self):
        try:
            title = self.page.title() or ""
            body = self.page.evaluate(
                "document.body && document.body.innerText || ''",
                isolated_context=False,
            ) or ""
            return "Access Denied" in title or ("Access Denied" in body and "edgesuite" in body.lower())
        except Exception:
            return False

    def _abck_blocked(self):
        if self.context is None:
            return False
        # Limit the check to cookies that Chrome would actually send to DOB NOW.
        # A persistent profile can contain other _abck cookies for unrelated
        # domains, and their order in context.cookies() is not significant.
        cookies = self.context.cookies([DOBNOW_URL])
        found = []
        for cookie in cookies:
            if cookie.get("name") != "_abck":
                continue
            parts = (cookie.get("value") or "").split("~", 2)
            status = parts[1] if len(parts) >= 2 else "unknown"
            found.append((cookie.get("domain"), cookie.get("path"), status))
        blocked = any(status == "-1" for _, _, status in found)
        if blocked:
            # Never log the cookie value; domain/path/status are enough to
            # diagnose duplicate or stale cookies safely.
            self.log.warning("_abck aplicables a DOB NOW (dominio, ruta, estado): %s", found)
        return blocked

    def wait_angular(self, timeout=120):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._page_access_denied():
                raise BlockedError("Access Denied visible")
            try:
                available = self.page.evaluate("""
                    typeof angular !== 'undefined' &&
                    angular.element(document.body).injector() !== undefined
                """, isolated_context=False)
                if available:
                    return True
            except Exception:
                pass
            time.sleep(1)
        return False

    def assert_healthy(self, require_angular=False):
        if self._page_access_denied():
            raise BlockedError("Access Denied visible")
        if self._abck_blocked():
            raise BlockedError("Sesion marcada como bloqueada (_abck=-1)")
        if require_angular and not self.wait_angular(120):
            raise RequestError("Angular/AuthTokenInterceptor no disponible")
        return True

    def reload_home(self):
        self.page.goto(DOBNOW_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)
        self.assert_healthy(require_angular=True)

    def request_json(self, method, path, body=None):
        self.assert_healthy(require_angular=True)
        try:
            result = self.page.evaluate("""
                async ({method, path, body}) => {
                    const injector = angular.element(document.body).injector();
                    const interceptor = injector.get('AuthTokenInterceptor');
                    let req = {method: method, url: path, headers: {}};
                    req = interceptor.request(req);
                    const options = {
                        method: method,
                        headers: {
                            'Content-Type': 'application/json',
                            'X-Requested-With': 'XMLHttpRequest',
                            ...(req.headers || {})
                        }
                    };
                    if (body !== null && body !== undefined && method !== 'GET') {
                        options.body = JSON.stringify(body);
                    }
                    const response = await fetch('https://a810-dobnow.nyc.gov' + path, options);
                    const text = await response.text();
                    return {status: response.status, text};
                }
            """, {"method": method, "path": path, "body": body}, isolated_context=False)
        except Exception as exc:
            message = str(exc)
            if "Failed to fetch" in message:
                raise RequestError("FETCH_FAILED") from exc
            raise RequestError(message[:500]) from exc
        status = int(result.get("status") or 0)
        raw = result.get("text") or ""
        if status == 403 or "Access Denied" in raw:
            raise BlockedError(f"Access Denied HTTP {status}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RequestError(f"Respuesta no JSON HTTP {status}: {raw[:200]}") from exc
        if status != 200:
            raise RequestError(f"HTTP {status}: {str(data)[:300]}")
        return data

    def search_bin(self, bin_num, street=""):
        data = self.request_json(
            "POST",
            f"{PUBLIC_PATH}/getPublicPortalBuildDisplay",
            {"BIN": bin_num, "SearchBy": "2", "StreetName": street},
        )
        if not data.get("IsSuccess"):
            raise RequestError(f"search_bin IsSuccess=false: {str(data)[:300]}")
        return data.get("ListBuildDetails") or [], data

    def get_pw1(self, guid):
        data = self.request_json("GET", f"{SERVICE_PATH}/GetJobFilingPW1/{guid}")
        return {
            "FilingIncludes": data.get("FilingIncludes", ""),
            "CurrentFilingStatusValue": data.get("CurrentFilingStatusValue", ""),
            "IsPlanApproved": data.get("IsPlanApproved", False),
            "raw": data,
        }

    def get_zd1wd(self, guid):
        data = self.request_json(
            "POST",
            f"{SERVICE_PATH}/GetPartialJobFilingServiceZD1WD",
            {"RelatedEntityLogicalName": "dobnyc_documentlist", "JobFilingGUID": guid},
        )
        return data.get("RequiredDocumentList") or []

    def get_portal_documents(self, guid, pw1):
        data = self.request_json(
            "POST",
            f"{PUBLIC_PATH}/GetPublicPortalPartialJobFiling",
            {
                "Applicant": None,
                "RelatedEntityLogicalName": "dobnyc_documentlist",
                "JobFilingGUID": guid,
                "FilingIncludes": pw1.get("FilingIncludes", ""),
                "CurrentFilingStatusValue": pw1.get("CurrentFilingStatusValue", ""),
                "IsPlanApproved": pw1.get("IsPlanApproved", False),
            },
        )
        return data.get("RequiredDocumentList") or []

    def get_download_url(self, document_url, borough):
        borough_key = BOROUGH_MAP.get(borough, (borough or "").upper())
        data = self.request_json(
            "POST",
            f"{SERVICE_PATH}/downloadFromDocumentum",
            {
                "uploadedPath": document_url,
                "downloadPath": f"\\\\PortalDownloadedDocuments\\{borough_key}\\TEST\\",
            },
        )
        path = str(data.get("downloadPath") or "")
        if not path:
            raise RequestError(f"downloadFromDocumentum sin downloadPath: {str(data)[:300]}")
        return path
