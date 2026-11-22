import base64
from io import BytesIO
import socket
import unittest
from unittest.mock import patch

from PIL import Image

from intelligence import images, network


ARTICLE = 'https://agenciabrasil.ebc.com.br/saude/noticia/2026-11/sample'
PHOTO = 'https://agenciabrasil.ebc.com.br/sites/default/files/photo.png'


CAMARA_ARTICLE = 'https://www.camara.leg.br/noticias/123-sample'
CAMARA_PHOTO = 'https://www.camara.leg.br/midias/image/photo.png'
CREDIT = 'João Silva / Câmara dos Deputados'


class ImageTransportTests(unittest.TestCase):
    def test_formats_are_normalized_with_visible_credit(self):
        for file_format, mode in [('PNG', 'RGBA'), ('JPEG', 'RGB'), ('WEBP', 'RGB')]:
            with self.subTest(file_format=file_format):
                raw = BytesIO()
                Image.new(mode, (1600, 800), 'white').save(raw, file_format, exif=b'Exif\0\0')
                with patch.object(images, 'read_public', return_value=(raw.getvalue(), CAMARA_PHOTO)):
                    result = images.prepare_image(CAMARA_PHOTO, article_url=CAMARA_ARTICLE,
                                                  credit=CREDIT, timeout=(2, 3))
                with Image.open(BytesIO(base64.b64decode(result, validate=True))) as output:
                    self.assertEqual(output.size, (480, 270))
                    self.assertFalse(output.getexif())
                    self.assertGreater(len(output.convert('L').crop((0, 222, 480, 270)).getcolors(256)), 1)

    def test_rejects_html_svg_corruption_and_excess_pixels(self):
        raw = BytesIO()
        Image.new('1', (9000, 1)).save(raw, 'PNG')
        for body in (b'<html>denied</html>', b'<svg/>', b'\xff\xd8broken', raw.getvalue()):
            with self.subTest(body=body[:15]):
                with patch.object(images, 'read_public', return_value=(body, CAMARA_PHOTO)):
                    with self.assertRaises(ValueError):
                        images.prepare_image(CAMARA_PHOTO, article_url=CAMARA_ARTICLE,
                                             credit=CREDIT, timeout=(2, 3))

    def test_uncredited_external_and_unapproved_media_are_never_downloaded(self):
        with patch.object(images, 'read_public') as fetch:
            for article, photo, credit in [
                (CAMARA_ARTICLE, CAMARA_PHOTO, ''), (CAMARA_ARTICLE, CAMARA_PHOTO, 'Reuters'),
                (CAMARA_ARTICLE, 'https://third-party.test/photo.jpg', CREDIT), (ARTICLE, PHOTO, CREDIT),
            ]:
                with self.subTest(article=article, photo=photo, credit=credit), self.assertRaises(ValueError):
                    images.prepare_image(photo, article_url=article, credit=credit, timeout=(2, 3))
        fetch.assert_not_called()

    def test_historical_metadata_requires_attribution_bound_to_the_photo(self):
        figure = (f'<figure data-midia="7" class="materia__midia"><img src="{CAMARA_PHOTO}">'
                  f'<figcaption><span class="materia__creditos-imagem">Foto: {CREDIT}</span></figcaption></figure>')
        for page, expected in [(figure, (CAMARA_PHOTO, CREDIT)),
                               (f'<meta property="og:image" content="{CAMARA_PHOTO}">', None)]:
            with patch.object(images, 'read_public', return_value=(page.encode(), CAMARA_ARTICLE)):
                self.assertEqual(images.image_reference(CAMARA_ARTICLE, timeout=(2, 3)), expected)


class PublicTransportTests(unittest.TestCase):
    def test_falls_back_to_a_second_public_address_on_connect_timeout(self):
        with patch.object(network.socket, 'getaddrinfo', return_value=[
            (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('2001:4860:4860::8888', 443, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443)),
        ]), patch.object(network, 'HTTPSConnectionPool') as connect:
            from unittest.mock import Mock
            first, second = Mock(), Mock()
            connect.side_effect = [first, second]
            first.urlopen.side_effect = OSError('Network unreachable')
            response = second.urlopen.return_value
            response.status = 200
            response.headers = {}
            response.read1.side_effect = [b'data', b'']
            self.assertEqual(network.read_public(ARTICLE, allowed_urls={ARTICLE}, max_bytes=100, timeout=(2, 3)),
                             (b'data', ARTICLE))
            self.assertEqual([call.args[0] for call in connect.call_args_list],
                             ['2001:4860:4860::8888', '8.8.8.8'])
            first.close.assert_called_once()
            second.close.assert_called_once()

    def test_private_dns_answer_rejected_before_connect(self):
        with patch.object(network.socket, 'getaddrinfo', return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443)),
        ]), patch.object(network, 'HTTPSConnectionPool') as connect:
            with self.assertRaisesRegex(ValueError, 'non-public'):
                network.read_public(ARTICLE, allowed_urls={ARTICLE}, max_bytes=100, timeout=(2, 3))
            connect.assert_not_called()

    def test_rejects_private_redirect_and_large_or_slow_responses(self):
        cases = (
            (302, {'Location': 'https://127.0.0.1/'}, (), ValueError),
            (200, {}, (b'a' * 101,), RuntimeError),
            (200, {}, (network.HTTPError('timeout'),), RuntimeError),
            (403, {}, (), RuntimeError),
        )
        for status, headers, chunks, error in cases:
            with self.subTest(status=status, headers=headers, chunks=chunks):
                with patch.object(network, '_public_addresses', return_value=['8.8.8.8']), \
                     patch.object(network, 'HTTPSConnectionPool') as connect:
                    response = connect.return_value.urlopen.return_value
                    response.status = status
                    response.headers = headers
                    response.read1.side_effect = chunks
                    with self.assertRaises(error):
                        network.read_public(ARTICLE, allowed_urls={ARTICLE}, max_bytes=100, timeout=(2, 3))
                    self.assertEqual(connect.call_args.args, ('8.8.8.8',))
                    self.assertEqual(connect.call_args.kwargs['server_hostname'], 'agenciabrasil.ebc.com.br')
                    response.close.assert_called_once()
