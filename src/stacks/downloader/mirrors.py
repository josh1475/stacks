import re
from urllib.parse import unquote


def _filename_from_disposition(value):
    """The filename a server named in its Content-Disposition header, or None.

    libgen.li buries HTML entities in odd shapes (an apostrophe arrives as
    "&_039_"); undo those too so filenames look like names again."""
    if not value:
        return None
    match = re.search(r'filename\*?=(?:\"([^\"]*)\"|([^;\s]+))', value)
    if not match:
        return None
    name = (match.group(1) or match.group(2)).strip()
    name = re.sub(r"&_\s*0?\s*39\s*_", "'", name)
    name = unquote(name)
    return name or None


def download_from_mirror(d, mirror_url, mirror_type, md5, title=None, resume_attempts=3, subfolder=None):
    """
    Download from any mirror with stale cookie handling.

    Logic:
    - slow_download: Use pre-warmed cookies with direct HTTP requests
    - libgen_get: The link redirects to a CDN that streams the file itself
    - external_mirror: Try direct, use FlareSolverr on 403 (with cookie refresh)

    Args:
        subfolder: Subfolder path to save file to (optional)
    """
    try:
        if mirror_type == 'slow_download':
            d.logger.debug("Accessing slow download (via cookies)")

            # Try to load cached cookies for this domain (uses current working domain)
            d.load_cached_cookies()

            if hasattr(d, 'status_callback'):
                d.status_callback("Accessing slow download page...")

            try:
                # Try to fetch the slow_download page with cookies
                response = d.session.get(mirror_url, timeout=30)

                # If we get a challenge page (403/503), solve it with FlareSolverr
                if response.status_code in [403, 503]:
                    if not d.flaresolverr_url:
                        d.logger.warning(f"Got {response.status_code} but no FlareSolverr configured")
                        return None

                    d.logger.warning(f"Got {response.status_code}, solving challenge with FlareSolverr...")

                    if hasattr(d, 'status_callback'):
                        d.status_callback("Solving CAPTCHA with FlareSolverr...")

                    # Solve challenge for THIS specific URL
                    success, cookies, html_content = d.solve_with_flaresolverr(mirror_url)

                    if not success:
                        d.logger.error("FlareSolverr failed")
                        return None

                    if hasattr(d, 'status_callback'):
                        d.status_callback("Extracting download link...")

                    download_link = d.parse_download_link_from_html(html_content, md5, mirror_url)
                    if not download_link:
                        d.logger.warning("Could not find download link")
                        return None

                    if hasattr(d, 'status_callback'):
                        d.status_callback("Downloading file...")

                    d.logger.info("Found download URL via FlareSolverr, downloading...")
                    return d.download_direct(download_link, title=title, resume_attempts=resume_attempts, md5=md5, subfolder=subfolder)

                response.raise_for_status()

                if hasattr(d, 'status_callback'):
                    d.status_callback("Extracting download link...")

                download_link = d.parse_download_link_from_html(response.text, md5, mirror_url)
                if not download_link:
                    d.logger.warning("Could not find download link")
                    return None

                if hasattr(d, 'status_callback'):
                    d.status_callback("Downloading file...")

                d.logger.info("Found download URL, downloading...")
                return d.download_direct(download_link, title=title, resume_attempts=resume_attempts, md5=md5, subfolder=subfolder)

            except Exception as e:
                d.logger.error(f"Error accessing slow_download page: {e}")
                return None

        elif mirror_type == 'libgen_get':
            # Library Genesis' keyed get.php link redirects to its CDN, which
            # streams the file itself and names it in Content-Disposition.
            # (Josh, 2026-11-21: "strive to improve it" — this is the only
            # mirror network that still answers from his server.)
            d.logger.debug(f"Streaming from Library Genesis: {mirror_url}")
            if hasattr(d, 'status_callback'):
                d.status_callback("Downloading file...")

            named = None
            try:
                # Headers only: stream=True stops before the body, so the file
                # isn't pulled twice — download_direct fetches it for real.
                probe = d.session.get(mirror_url, timeout=30, stream=True)
                disposition = probe.headers.get('Content-Disposition', '') if probe.ok else ''
                probe.close()
                named = _filename_from_disposition(disposition)
            except Exception as e:
                d.logger.warning(f"Could not read Library Genesis headers: {e}")

            return d.download_direct(mirror_url, title=named or title, resume_attempts=resume_attempts, md5=md5, subfolder=subfolder)

        else:  # external_mirror
            d.logger.debug(f"Accessing external mirror: {mirror_url}")

            # Try to load cached cookies for this mirror
            d.load_cached_cookies(domain=mirror_url)

            try:
                response = d.session.get(mirror_url, timeout=30)

                # If 403, refresh cookies and retry
                if response.status_code == 403:
                    if d.flaresolverr_url:
                        d.logger.warning("Got 403 - trying to refresh cookies")

                        # Try to pre-warm new cookies
                        if d.prewarm_cookies():
                            d.logger.info("Retrying with fresh cookies...")
                            # Retry once with fresh cookies
                            response = d.session.get(mirror_url, timeout=30)

                            if response.status_code == 403:
                                d.logger.warning("Still got 403 after cookie refresh, using FlareSolverr for full solve")
                            else:
                                # Success with fresh cookies, continue to parse
                                response.raise_for_status()

                                if hasattr(d, 'status_callback'):
                                    d.status_callback("Extracting download link...")

                                download_link = d.parse_download_link_from_html(response.text, md5, mirror_url)
                                if not download_link:
                                    d.logger.warning("Could not find download link")
                                    return None

                                if hasattr(d, 'status_callback'):
                                    d.status_callback("Downloading file...")

                                return d.download_direct(download_link, title=title, resume_attempts=resume_attempts, md5=md5, subfolder=subfolder)

                        # If cookie refresh failed or still got 403, use FlareSolverr
                        if hasattr(d, 'status_callback'):
                            d.status_callback("Solving CAPTCHA with FlareSolverr...")
                        success, cookies, html_content = d.solve_with_flaresolverr(mirror_url)

                        if success:
                            if hasattr(d, 'status_callback'):
                                d.status_callback("Extracting download link...")
                            download_link = d.parse_download_link_from_html(html_content, md5, mirror_url)
                            if download_link:
                                if hasattr(d, 'status_callback'):
                                    d.status_callback("Downloading file...")
                                d.logger.info("Found download URL via FlareSolverr, downloading...")
                                return d.download_direct(download_link, title=title, resume_attempts=resume_attempts, md5=md5, subfolder=subfolder)
                        return None
                    else:
                        d.logger.warning("Got 403 but FlareSolverr not configured")
                        return None

                response.raise_for_status()

                if hasattr(d, 'status_callback'):
                    d.status_callback("Extracting download link...")

                download_link = d.parse_download_link_from_html(response.text, md5, mirror_url)
                if not download_link:
                    d.logger.warning("Could not find download link")
                    return None

                if hasattr(d, 'status_callback'):
                    d.status_callback("Downloading file...")

                return d.download_direct(download_link, title=title, resume_attempts=resume_attempts, md5=md5, subfolder=subfolder)

            except Exception as e:
                d.logger.error(f"Error accessing external mirror: {e}")
                return None
    
    except Exception as e:
        d.logger.error(f"Error downloading from mirror: {e}")
        return None