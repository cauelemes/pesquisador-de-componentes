"""O baixador seguro de imagens (sem rede, sem DNS de verdade, sem créditos).

    python -m unittest tests.test_download_seguro -v

O DNS e o servidor são falsos: um `resolver` que devolve os IPs que o teste quer e um `httpx.MockTransport` que responde
no lugar da internet. Assim dá para provar o que o baixador RECUSA (a parte que importa para a segurança) sem
tocar a rede.
"""

import gzip
import itertools
import socket
import unittest

import httpx

from backend.compartilhado import download_seguro as dl
from backend.compartilhado.download_seguro import ErroDeDownload

PUBLICO = "93.184.216.34"
OUTRO_PUBLICO = "93.184.216.35"
CORPO = b"bytes-da-imagem"
DNS = {
    "img.exemplo.com": [PUBLICO],
    "cdn.exemplo.com": [OUTRO_PUBLICO],
    "interno.exemplo.com": ["10.0.0.5"],
    "misto.exemplo.com": [PUBLICO, "10.0.0.5"],
}


def resolvedor(tabela=None):
    tabela = DNS if tabela is None else tabela

    def resolver(host, porta):
        if host not in tabela:
            raise socket.gaierror("sem DNS")
        return list(tabela[host])

    return resolver


def imagem(conteudo=CORPO, tipo="image/png", **cabecalhos):
    return httpx.Response(200, content=conteudo, headers={"content-type": tipo, **cabecalhos})


class Servidor:
    """O "servidor" falso: guarda os pedidos recebidos e responde pelo (nome do site, caminho)."""

    def __init__(self, respostas):
        self.respostas = respostas  # {(host, caminho): Response | função(pedido) -> Response}
        self.pedidos = []

    def __call__(self, pedido):
        self.pedidos.append(pedido)
        resposta = self.respostas[(pedido.headers["host"], pedido.url.path)]
        return resposta(pedido) if callable(resposta) else resposta


def baixar(url, respostas, *, resolver=None, **extra):
    servidor = Servidor(respostas)
    resultado = dl.baixar_imagem(url, resolver=resolver or resolvedor(), transporte=httpx.MockTransport(servidor), **extra)
    return resultado, servidor


class TestIpPublico(unittest.TestCase):
    def test_ips_de_internet_pública_passam(self):
        for ip in ("93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"):
            self.assertTrue(dl.ip_publico(ip), ip)

    def test_redes_internas_e_especiais_sao_recusadas(self):
        recusados = (
            "10.0.0.1", "172.16.0.1", "192.168.1.1",  # redes privadas
            "127.0.0.1", "::1",  # a própria máquina
            "169.254.169.254",  # os metadados da AWS (credenciais da instância)
            "100.64.0.1",  # CGNAT
            "0.0.0.0", "255.255.255.255", "224.0.0.1",  # inespecífico, broadcast, multicast
            "fe80::1", "fe80::1%eth0", "fd00::1",  # link-local e ULA do IPv6
            "::ffff:127.0.0.1", "::ffff:8.8.8.8",  # IPv4 escondido dentro de IPv6
            "64:ff9b::808:808", "2002:7f00:1::",  # tradução NAT64 e 6to4
            "isto-nao-e-um-ip", "",
        )
        for ip in recusados:
            self.assertFalse(dl.ip_publico(ip), ip)


class TestBaixarComSucesso(unittest.TestCase):
    def test_baixa_pelo_ip_validado_com_o_nome_do_site_no_cabecalho(self):
        resultado, servidor = baixar("http://img.exemplo.com/fotos/a.png?x=1", {("img.exemplo.com", "/fotos/a.png"): imagem()})
        self.assertEqual(resultado.conteudo, CORPO)
        self.assertEqual(resultado.tipo_de_conteudo, "image/png")
        self.assertEqual(resultado.url_final, "http://img.exemplo.com/fotos/a.png?x=1")
        (pedido,) = servidor.pedidos
        self.assertEqual(pedido.url.host, PUBLICO)  # conectou no IP já conferido, não no nome
        self.assertIn(pedido.url.port, (None, 80))  # o httpx guarda a porta padrão como None
        self.assertEqual(pedido.url.query, b"x=1")
        self.assertEqual(pedido.headers["host"], "img.exemplo.com")
        self.assertNotIn("cookie", pedido.headers)

    def test_https_conecta_no_ip_mas_confere_o_certificado_do_nome(self):
        _, servidor = baixar("https://img.exemplo.com/a.png", {("img.exemplo.com", "/a.png"): imagem()})
        (pedido,) = servidor.pedidos
        self.assertEqual(pedido.url.host, PUBLICO)
        self.assertIn(pedido.url.port, (None, 443))
        self.assertEqual(pedido.url.scheme, "https")
        self.assertEqual(pedido.extensions["sni_hostname"], "img.exemplo.com")  # o TLS valida o certificado deste nome

    def test_ipv6_vai_entre_colchetes(self):
        resolver = resolvedor({"v6.exemplo.com": ["2606:4700:4700::1111"]})
        _, servidor = baixar("http://v6.exemplo.com/a.png", {("v6.exemplo.com", "/a.png"): imagem()}, resolver=resolver)
        self.assertEqual(servidor.pedidos[0].url.host, "2606:4700:4700::1111")

    def test_porta_diferente_da_padrao_do_esquema_vai_no_cabecalho(self):
        _, servidor = baixar("http://img.exemplo.com:443/a.png", {("img.exemplo.com:443", "/a.png"): imagem()})
        self.assertEqual(servidor.pedidos[0].url.port, 443)

    def test_tipo_generico_e_aceito_porque_quem_decide_e_o_conteudo(self):
        resultado, _ = baixar("http://img.exemplo.com/a", {("img.exemplo.com", "/a"): imagem(tipo="application/octet-stream")})
        self.assertEqual(resultado.conteudo, CORPO)

    def test_tenta_o_proximo_ip_se_a_conexao_com_o_primeiro_falhar(self):
        resolver = resolvedor({"img.exemplo.com": [PUBLICO, OUTRO_PUBLICO]})
        tentativas = []

        def handler(pedido):
            tentativas.append(pedido.url.host)
            if pedido.url.host == PUBLICO:
                raise httpx.ConnectError("recusou")
            return imagem()

        resultado = dl.baixar_imagem("http://img.exemplo.com/a.png", resolver=resolver, transporte=httpx.MockTransport(handler))
        self.assertEqual(resultado.conteudo, CORPO)
        self.assertEqual(tentativas, [PUBLICO, OUTRO_PUBLICO])


class TestEnderecosRecusados(unittest.TestCase):
    def assertRecusa(self, url, trecho, resolver=None):
        servidor = Servidor({})
        with self.assertRaises(ErroDeDownload) as contexto:
            dl.baixar_imagem(url, resolver=resolver or resolvedor(), transporte=httpx.MockTransport(servidor))
        self.assertIn(trecho, str(contexto.exception))
        self.assertEqual(servidor.pedidos, [], f"não podia nem tentar conectar: {url}")

    def test_esquemas_que_nao_sao_http(self):
        for url in ("file:///etc/passwd", "ftp://img.exemplo.com/a.png", "data:image/png;base64,AAAA", "javascript:alert(1)", "gopher://img.exemplo.com/"):
            self.assertRecusa(url, "http")

    def test_usuario_e_senha_no_endereco(self):
        self.assertRecusa("http://usuario:senha@img.exemplo.com/a.png", "usuário e senha")
        self.assertRecusa("http://img.exemplo.com@interno.exemplo.com/a.png", "usuário e senha")

    def test_ip_numerico_inclusive_o_de_metadados_da_aws(self):
        for url in ("http://127.0.0.1/a.png", "http://[::1]/a.png", "http://169.254.169.254/latest/meta-data/", "http://8.8.8.8/a.png"):
            self.assertRecusa(url, "IP numérico")

    def test_nomes_que_podem_ser_ips_escondidos_ou_internos(self):
        for url in ("http://2130706433/a.png", "http://0x7f.1/a.png", "http://localhost/a.png", "http://intranet/a.png"):
            self.assertRecusa(url, "nome de site válido")

    def test_portas_fora_de_80_e_443(self):
        for url in ("http://img.exemplo.com:8080/a.png", "http://img.exemplo.com:22/a.png", "https://img.exemplo.com:6379/a.png"):
            self.assertRecusa(url, "portas 80 e 443")

    def test_vazio_grande_demais_e_caracteres_de_controle(self):
        self.assertRecusa("", "vazio")
        self.assertRecusa("   ", "vazio")
        self.assertRecusa("http://img.exemplo.com/" + "a" * 2000, "inválido")
        self.assertRecusa("http://img.exemplo.com/a b.png", "inválido")
        self.assertRecusa("http://img.exemplo.com/a\r\nHost: outro", "inválido")
        self.assertRecusa("http://img.exemplo.com/a\\b", "inválido")

    def test_nome_que_resolve_para_ip_interno(self):
        for ip in ("10.0.0.5", "127.0.0.1", "169.254.169.254", "192.168.0.1", "::1", "fd00::1", "100.64.0.1", "::ffff:127.0.0.1"):
            self.assertRecusa("http://rebind.exemplo.com/a.png", "rede não pública", resolver=resolvedor({"rebind.exemplo.com": [ip]}))

    def test_basta_um_ip_interno_entre_os_devolvidos(self):
        self.assertRecusa("http://misto.exemplo.com/a.png", "rede não pública")

    def test_dns_que_falha_ou_volta_vazio(self):
        self.assertRecusa("http://nao-existe.exemplo.com/a.png", "DNS")
        self.assertRecusa("http://vazio.exemplo.com/a.png", "DNS", resolver=resolvedor({"vazio.exemplo.com": []}))


class TestRedirecionamentos(unittest.TestCase):
    def test_segue_um_redirecionamento_para_outro_site_publico(self):
        respostas = {
            ("img.exemplo.com", "/a"): httpx.Response(302, headers={"location": "https://cdn.exemplo.com/b.png"}),
            ("cdn.exemplo.com", "/b.png"): imagem(),
        }
        resultado, servidor = baixar("http://img.exemplo.com/a", respostas)
        self.assertEqual(resultado.url_final, "https://cdn.exemplo.com/b.png")
        self.assertEqual([p.url.host for p in servidor.pedidos], [PUBLICO, OUTRO_PUBLICO])

    def test_redirecionamento_relativo(self):
        respostas = {
            ("img.exemplo.com", "/a"): httpx.Response(301, headers={"location": "/novo/b.png"}),
            ("img.exemplo.com", "/novo/b.png"): imagem(),
        }
        resultado, _ = baixar("http://img.exemplo.com/a", respostas)
        self.assertEqual(resultado.url_final, "http://img.exemplo.com/novo/b.png")

    def test_redirecionamento_para_rede_interna_e_recusado_sem_conectar(self):
        for destino in ("http://interno.exemplo.com/b.png", "http://169.254.169.254/latest/meta-data/", "file:///etc/passwd", "http://img.exemplo.com:8080/b.png"):
            respostas = {("img.exemplo.com", "/a"): httpx.Response(302, headers={"location": destino})}
            with self.assertRaises(ErroDeDownload, msg=destino):
                baixar("http://img.exemplo.com/a", respostas)

    def test_so_o_primeiro_pedido_sai_quando_o_segundo_destino_e_proibido(self):
        servidor = Servidor({("img.exemplo.com", "/a"): httpx.Response(302, headers={"location": "http://interno.exemplo.com/b.png"})})
        with self.assertRaises(ErroDeDownload):
            dl.baixar_imagem("http://img.exemplo.com/a", resolver=resolvedor(), transporte=httpx.MockTransport(servidor))
        self.assertEqual(len(servidor.pedidos), 1)

    def test_no_maximo_3_redirecionamentos(self):
        def salto(n):
            return httpx.Response(302, headers={"location": f"/{n}"})

        respostas = {("img.exemplo.com", f"/{n}"): salto(n + 1) for n in range(10)}
        respostas[("img.exemplo.com", "/a")] = salto(0)
        with self.assertRaisesRegex(ErroDeDownload, "redirecionamentos demais"):
            baixar("http://img.exemplo.com/a", respostas)
        respostas = {
            ("img.exemplo.com", "/a"): salto(1), ("img.exemplo.com", "/1"): salto(2), ("img.exemplo.com", "/2"): salto(3),
            ("img.exemplo.com", "/3"): imagem(),
        }
        self.assertEqual(baixar("http://img.exemplo.com/a", respostas)[0].conteudo, CORPO)  # 3 saltos ainda passam

    def test_redirecionamento_sem_destino(self):
        with self.assertRaisesRegex(ErroDeDownload, "sem destino"):
            baixar("http://img.exemplo.com/a", {("img.exemplo.com", "/a"): httpx.Response(302)})


def em_pedacos(*pedacos):
    """Um corpo em fluxo (o httpx só o lê quando o baixador pede, como numa resposta de verdade)."""
    return iter(pedacos)


class TestRespostasRecusadas(unittest.TestCase):
    def confere(self, resposta, trecho, **extra):
        with self.assertRaises(ErroDeDownload) as contexto:
            baixar("http://img.exemplo.com/a", {("img.exemplo.com", "/a"): resposta}, **extra)
        self.assertIn(trecho, str(contexto.exception))

    def test_erro_http(self):
        self.confere(httpx.Response(404), "HTTP 404")
        self.confere(httpx.Response(500), "HTTP 500")

    def test_tipo_que_nao_e_imagem(self):
        self.confere(imagem(b"<html>", tipo="text/html; charset=utf-8"), "não devolveu uma imagem")
        self.confere(httpx.Response(200, content=CORPO), "não devolveu uma imagem")  # sem tipo nenhum

    def test_svg_e_recusado_logo(self):
        self.confere(imagem(b"<svg/>", tipo="image/svg+xml"), "SVG")

    def test_tamanho_declarado_grande_demais(self):
        self.confere(imagem(b"x" * 2000), "grande demais", max_bytes=1000)

    def test_tamanho_real_grande_demais_mesmo_sem_tamanho_declarado(self):
        self.confere(imagem(em_pedacos(*[b"x" * 300] * 10)), "grande demais", max_bytes=1000)

    def test_bomba_gzip_conta_o_tamanho_depois_de_descomprimir(self):
        comprimido = gzip.compress(b"0" * 2_000_000)
        self.assertLess(len(comprimido), 10_000)  # pequeno no fio, enorme na memória
        self.confere(imagem(em_pedacos(comprimido), **{"content-encoding": "gzip"}), "grande demais", max_bytes=100_000)

    def test_resposta_vazia(self):
        self.confere(imagem(b""), "vazia")

    def test_gzip_quebrado(self):
        self.confere(imagem(em_pedacos(b"isto nao e gzip"), **{"content-encoding": "gzip"}), "não pôde ser lida")

    def test_prazo_total_do_download(self):
        contador = itertools.count()
        relogio = lambda: next(contador) * 12.0  # cada consulta ao relógio "gasta" 12 s
        self.confere(imagem(em_pedacos(*[b"x" * 10] * 5)), "demorou demais", relogio=relogio)


class TestFalhasDeRede(unittest.TestCase):
    def falha(self, erro, trecho):
        def handler(pedido):
            raise erro

        with self.assertRaises(ErroDeDownload) as contexto:
            dl.baixar_imagem("http://img.exemplo.com/a", resolver=resolvedor(), transporte=httpx.MockTransport(handler))
        self.assertIn(trecho, str(contexto.exception))

    def test_tempo_esgotado(self):
        self.falha(httpx.ConnectTimeout("lento"), "a tempo")
        self.falha(httpx.ReadTimeout("lento"), "a tempo")

    def test_conexao_recusada(self):
        self.falha(httpx.ConnectError("recusou"), "conectar")


if __name__ == "__main__":
    unittest.main()
