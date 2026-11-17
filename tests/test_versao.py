"""A versão do sistema (sem rede, sem créditos).

    python -m unittest tests.test_versao -v
"""

import re
import unittest

from backend.compartilhado.versao import VERSAO

# SemVer: MAIOR.MENOR.CORREÇÃO, com sufixo opcional ("-beta", "-beta.dev"...).
SEMVER = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z]+(\.[0-9A-Za-z]+)*)?$")


class TestVersao(unittest.TestCase):
    def test_segue_o_padrao_semver(self):
        self.assertRegex(VERSAO, SEMVER)

    def test_padrao_recusa_formatos_errados(self):
        for errada in ("0.1", "v0.1.0", "0.1.0-", "0.1.0 beta"):
            with self.subTest(errada=errada):
                self.assertNotRegex(errada, SEMVER)
