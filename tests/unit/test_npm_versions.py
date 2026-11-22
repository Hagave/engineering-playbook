"""
How the template tests pick what a new project installs: the `latest` tag as
is when it is stable (npm then resolves it against the other packages' peers),
the newest stable release when the tag is a prerelease, and the anchor's exact
release for a coupled package and its anchor.
"""
import unittest
from unittest import mock

from tests.e2e import npm_versions
from tests.e2e.npm_versions import install_spec, pick_release, resolve

_PRISMA_VIEWS = {
    '@prisma/client': ({'latest': '7.10.1', 'dev': '8.1.0-dev.12'}, ['7.9.0', '7.10.1', '8.1.0-dev.12']),
    'prisma': ({'latest': '8.0.0-rc.34'}, ['7.10.1', '8.0.0-rc.34']),
    '@prisma/adapter-pg': ({'latest': '7.10.1'}, ['7.10.1']),
    'typescript': ({'latest': '7.0.2'}, ['6.0.3', '7.0.2']),
}


class TestPickRelease(unittest.TestCase):

    def test_a_stable_latest_tag_is_taken(self) -> None:
        self.assertEqual(pick_release({'latest': '7.10.1'}, ['7.10.1', '8.0.0-rc.34']), '7.10.1')

    def test_a_prerelease_latest_tag_falls_back_to_the_newest_stable(self) -> None:
        versions = ['6.19.3', '7.9.0', '7.10.1', '7.10.0-integration.1', '8.0.0-rc.34']
        self.assertEqual(pick_release({'latest': '8.0.0-rc.34'}, versions), '7.10.1')

    def test_the_newest_stable_compares_numerically(self) -> None:
        self.assertEqual(pick_release({'latest': '2.0.0-beta.1'}, ['1.9.0', '1.10.0', '1.2.0']), '1.10.0')

    def test_a_stable_latest_wins_over_a_higher_stable_under_another_tag(self) -> None:
        self.assertEqual(pick_release({'latest': '5.4.0', 'next': '6.0.0'}, ['5.4.0', '6.0.0']), '5.4.0')

    def test_no_stable_release_fails_loud(self) -> None:
        with self.assertRaisesRegex(ValueError, 'no stable release'):
            pick_release({'latest': '1.0.0-rc.1'}, ['1.0.0-rc.1', '1.0.0-alpha.3'])

    def test_a_missing_latest_tag_falls_back_to_the_newest_stable(self) -> None:
        self.assertEqual(pick_release({}, ['0.9.0', '1.0.0']), '1.0.0')


class TestInstallSpec(unittest.TestCase):

    def test_a_stable_latest_tag_goes_in_as_the_tag_so_npm_can_honor_peers(self) -> None:
        self.assertEqual(install_spec({'latest': '7.0.2'}, ['6.0.3', '7.0.2']), 'latest')

    def test_a_prerelease_latest_tag_goes_in_as_the_newest_stable_version(self) -> None:
        self.assertEqual(install_spec({'latest': '8.0.0-rc.34'}, ['7.10.1', '8.0.0-rc.34']), '7.10.1')


class TestResolve(unittest.TestCase):

    def _resolve(self, names: list[str], coupled: dict[str, str]) -> tuple[dict[str, str], mock.MagicMock]:
        with mock.patch.object(npm_versions, '_view', side_effect=_PRISMA_VIEWS.__getitem__) as view:
            return resolve(names, coupled), view

    def test_a_coupled_package_and_its_anchor_take_the_anchor_release_exactly(self) -> None:
        specs, view = self._resolve(['@prisma/client', 'prisma', '@prisma/adapter-pg'],
                                    {'prisma': '@prisma/client', '@prisma/adapter-pg': '@prisma/client'})
        self.assertEqual(specs, dict.fromkeys(('@prisma/client', 'prisma', '@prisma/adapter-pg'), '7.10.1'))
        view.assert_called_once_with('@prisma/client')

    def test_an_uncoupled_package_with_a_stable_tag_keeps_the_tag(self) -> None:
        specs, _ = self._resolve(['typescript'], {})
        self.assertEqual(specs, {'typescript': 'latest'})

    def test_an_uncoupled_package_with_a_prerelease_tag_gets_the_newest_stable(self) -> None:
        specs, _ = self._resolve(['prisma'], {})
        self.assertEqual(specs, {'prisma': '7.10.1'})

    def test_an_anchor_outside_the_list_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, 'anchors not in the package list'):
            resolve(['prisma'], {'prisma': '@prisma/client'})


if __name__ == '__main__':
    unittest.main()
