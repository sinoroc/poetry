import logging
import os
import tarfile
import zipfile

import pkginfo

from bz2 import BZ2File
from gzip import GzipFile
from typing import List
from typing import Union

try:
    import urllib.parse as urlparse
except ImportError:
    import urlparse

try:
    from xmlrpc.client import ServerProxy
except ImportError:
    from xmlrpclib import ServerProxy

from cachecontrol import CacheControl
from cachecontrol.caches.file_cache import FileCache
from cachy import CacheManager
from requests import get
from requests import session

from poetry.locations import CACHE_DIR
from poetry.packages import dependency_from_pep_508
from poetry.packages import Package
from poetry.semver import parse_constraint
from poetry.semver import VersionConstraint
from poetry.utils._compat import Path
from poetry.utils._compat import to_str
from poetry.utils.helpers import parse_requires
from poetry.utils.helpers import temporary_directory
from poetry.version.markers import InvalidMarker

from .repository import Repository


logger = logging.getLogger(__name__)


class PyPiRepository(Repository):

    def __init__(self,
                 url='https://pypi.org/',
                 disable_cache=False,
                 fallback=True):
        self._name = 'PyPI'
        self._url = url
        self._disable_cache = disable_cache
        self._fallback = fallback

        release_cache_dir = Path(CACHE_DIR) / 'cache' / 'repositories' / 'pypi'
        self._cache = CacheManager({
            'default': 'releases',
            'serializer': 'json',
            'stores': {
                'releases': {
                    'driver': 'file',
                    'path': str(release_cache_dir)
                },
                'packages': {
                    'driver': 'dict'
                }
            }
        })

        self._session = CacheControl(
            session(),
            cache=FileCache(str(release_cache_dir / '_http'))
        )
        
        super(PyPiRepository, self).__init__()

    def find_packages(self,
                      name,                    # type: str
                      constraint=None,         # type: Union[VersionConstraint, str, None]
                      extras=None,             # type: Union[list, None]
                      allow_prereleases=False  # type: bool
                      ):  # type: (...) -> List[Package]
        """
        Find packages on the remote server.
        """
        if constraint is not None and not isinstance(constraint, VersionConstraint):
            constraint = parse_constraint(constraint)

        info = self.get_package_info(name)

        packages = []

        for version, release in info['releases'].items():
            if not release:
                # Bad release
                self._log(
                    'No release information found for {}-{}, skipping'.format(
                        name, version
                    ),
                    level='debug'
                )
                continue

            package = Package(name, version)

            if package.is_prerelease() and not allow_prereleases:
                continue

            if (
                not constraint
                or (constraint and constraint.allows(package.version))
            ):
                if extras is not None:
                    package.requires_extras = extras

                packages.append(package)

        self._log(
            '{} packages found for {} {}'.format(
                len(packages), name, str(constraint)
            ),
            level='debug'
        )

        return packages

    def package(self,
                name,        # type: str
                version,     # type: str
                extras=None  # type: (Union[list, None])
                ):  # type: (...) -> Union[Package, None]
        try:
            index = self._packages.index(Package(name, version, version))

            return self._packages[index]
        except ValueError:
            if extras is None:
                extras = []

            release_info = self.get_release_info(name, version)
            if (
                self._fallback
                and release_info['requires_dist'] is None
                and not release_info['requires_python']
                and '_fallback' not in release_info
            ):
                # Force cache update
                self._log(
                    'No dependencies found, downloading archives',
                    level='debug'
                )
                self._cache.forget('{}:{}'.format(name, version))
                release_info = self.get_release_info(name, version)

            package = Package(name, version, version)
            requires_dist = release_info['requires_dist'] or []
            for req in requires_dist:
                try:
                    dependency = dependency_from_pep_508(req)
                except InvalidMarker:
                    # Invalid marker
                    # We strip the markers hoping for the best
                    req = req.split(';')[0]

                    dependency = dependency_from_pep_508(req)
                except ValueError:
                    # Likely unable to parse constraint so we skip it
                    self._log(
                        'Invalid constraint ({}) found in {}-{} dependencies, '
                        'skipping'.format(
                            req, package.name, package.version
                        ),
                        level='debug'
                    )
                    continue

                if dependency.extras:
                    for extra in dependency.extras:
                        if extra not in package.extras:
                            package.extras[extra] = []

                        package.extras[extra].append(dependency)

                if not dependency.is_optional():
                    package.requires.append(dependency)

            # Adding description
            package.description = release_info.get('summary', '')

            if release_info['requires_python']:
                package.python_versions = release_info['requires_python']

            if release_info['platform']:
                package.platform = release_info['platform']

            # Adding hashes information
            package.hashes = release_info['digests']

            # Activate extra dependencies
            for extra in extras:
                if extra in package.extras:
                    for dep in package.extras[extra]:
                        dep.activate()

                    package.requires += package.extras[extra]

            self._packages.append(package)

            return package

    def search(self, query, mode=0):
        results = []

        search = {
            'name': query
        }

        if mode == self.SEARCH_FULLTEXT:
            search['summary'] = query

        client = ServerProxy('https://pypi.python.org/pypi')
        hits = client.search(search, 'or')

        for hit in hits:
            result = Package(hit['name'], hit['version'], hit['version'])
            result.description = to_str(hit['summary'])
            results.append(result)

        return results

    def get_package_info(self, name):  # type: (str) -> dict
        """
        Return the package information given its name.

        The information is returned from the cache if it exists
        or retrieved from the remote server.
        """
        if self._disable_cache:
            return self._get_package_info(name)

        return self._cache.store('packages').remember_forever(
            name,
            lambda: self._get_package_info(name)
        )

    def _get_package_info(self, name):  # type: (str) -> dict
        data = self._get('pypi/{}/json'.format(name))
        if data is None:
            raise ValueError('Package [{}] not found.'.format(name))

        return data

    def get_release_info(self, name, version):  # type: (str, str) -> dict
        """
        Return the release information given a package name and a version.

        The information is returned from the cache if it exists
        or retrieved from the remote server.
        """
        if self._disable_cache:
            return self._get_release_info(name, version)

        return self._cache.remember_forever(
            '{}:{}'.format(name, version),
            lambda: self._get_release_info(name, version)
        )

    def _get_release_info(self, name, version):  # type: (str, str) -> dict
        json_data = self._get('pypi/{}/{}/json'.format(name, version))
        if json_data is None:
            raise ValueError('Package [{}] not found.'.format(name))

        info = json_data['info']
        data = {
            'name': info['name'],
            'version': info['version'],
            'summary': info['summary'],
            'platform': info['platform'],
            'requires_dist': info['requires_dist'],
            'requires_python': info['requires_python'],
            'digests': [],
            '_fallback': False
        }

        try:
            version_info = json_data['releases'][version]
        except KeyError:
            version_info = []

        for file_info in version_info:
            data['digests'].append(file_info['digests']['sha256'])

        if (
                self._fallback
                and data['requires_dist'] is None
                and not data['requires_python']
        ):
            # No dependencies set (along with other information)
            # This might be due to actually no dependencies
            # or badly set metadata when uploading
            # So, we need to make sure there is actually no
            # dependencies by introspecting packages
            data['_fallback'] = True

            urls = {}
            for url in json_data['urls']:
                # Only get sdist and universal wheels
                dist_type = url['packagetype']
                if dist_type not in ['sdist', 'bdist_wheel']:
                    continue

                if dist_type == 'sdist' and 'dist' not in urls:
                    urls[url['packagetype']] = url['url']
                    continue

                if 'bdist_wheel' in urls:
                    continue

                # If bdist_wheel, check if it's universal
                python_version = url['python_version']
                if python_version not in ['py2.py3', 'py3', 'py2']:
                    continue

                parts = urlparse.urlparse(url['url'])
                filename = os.path.basename(parts.path)

                if '-none-any' not in filename:
                    continue

            if not urls:
                return data

            requires_dist = self._get_requires_dist_from_urls(urls)

            data['requires_dist'] = requires_dist

        return data

    def _get(self, endpoint):  # type: (str) -> Union[dict, None]
        json_response = self._session.get(self._url + endpoint)
        if json_response.status_code == 404:
            return None

        json_data = json_response.json()

        return json_data

    def _get_requires_dist_from_urls(self, urls
                                     ):  # type: (dict) -> Union[list, None]
        if 'bdist_wheel' in urls:
            return self._get_requires_dist_from_wheel(urls['bdist_wheek'])

        return self._get_requires_dist_from_sdist(urls['sdist'])

    def _get_requires_dist_from_wheel(self, url
                                      ):  # type: (str) -> Union[list, None]
        filename = os.path.basename(urlparse.urlparse(url).path)

        with temporary_directory() as temp_dir:
            filepath = os.path.join(temp_dir, filename)
            self._download(url, filepath)

            try:
                meta = pkginfo.Wheel(filepath)
            except ValueError:
                # Unable to determine dependencies
                # Assume none
                return

        if meta.requires_dist:
            return meta.requires_dist

    def _get_requires_dist_from_sdist(self, url
                                      ):  # type: (str) -> Union[list, None]
        filename = os.path.basename(urlparse.urlparse(url).path)

        with temporary_directory() as temp_dir:
            filepath = Path(temp_dir) / filename
            self._download(url, str(filepath))

            try:
                meta = pkginfo.SDist(str(filepath))

                if meta.requires_dist:
                    return meta.requires_dist
            except ValueError:
                # Unable to determine dependencies
                # We pass and go deeper
                pass

            # Still not dependencies found
            # So, we unpack and introspect
            suffix = filepath.suffix
            gz = None
            if suffix == '.zip':
                tar = zipfile.ZipFile(str(filepath))
            else:
                if suffix == '.bz2':
                    gz = BZ2File(str(filepath))
                else:
                    gz = GzipFile(str(filepath))

                tar = tarfile.TarFile(str(filepath), fileobj=gz)

            try:
                tar.extractall(os.path.join(temp_dir, 'unpacked'))
            finally:
                if gz:
                    gz.close()

                tar.close()

            unpacked = Path(temp_dir) / 'unpacked'
            sdist_dir = unpacked / Path(filename).name.rstrip('.tar.gz')

            # Checking for .egg-info
            eggs = list(sdist_dir.glob('*.egg-info'))
            if eggs:
                egg_info = eggs[0]

                requires = egg_info / 'requires.txt'
                if requires.exists():
                    with requires.open() as f:
                        return parse_requires(f.read())

                return

            # Still nothing, assume no dependencies
            # We could probably get them by executing
            # python setup.py egg-info but I don't feel
            # confortable executing a file just for the sake
            # of getting dependencies.
            return

    def _download(self, url, dest):  # type: (str, str) -> None
        r = get(url, stream=True)
        with open(dest, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1024):
                if chunk:
                    f.write(chunk)

    def _log(self, msg, level='info'):
        getattr(logger, level)('{}: {}'.format(self._name, msg))
