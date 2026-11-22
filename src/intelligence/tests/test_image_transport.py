import base64
from io import BytesIO
import socket
import unittest
from unittest.mock import patch

from PIL import Image

from intelligence import images, network


ARTICLE = 'https://agenciabrasil.ebc.com.br/saude/noticia/2026-11/sample'
PHOTO = 'https://agenciabrasil.ebc.com.br/sites/default/files/photo.png'
PERMISSIONS = {ARTICLE: PHOTO}


class ImageTransportTests(unittest.TestCase):
    def test_formats_are_normalized_and_metadata_removed(self):
        for file_format, mode in [('PNG', 'RGBA'), ('JPEG', 'RGB'), ('WEBP', 'RGB')]:
            with self.subTest(file_format=file_format):
                raw = BytesIO()
                Image.new(mode, (1600, 800)).save(raw, file_format, exif=b'Exif\0\0')
                with patch.object(images, 'read_public', return_value=(raw.getvalue(), PHOTO)):
                    result = images.prepare_image(None, PHOTO, article_url=ARTICLE,
                                                  permissions=PERMISSIONS, timeout=(2, 3))
                with Image.open(BytesIO(base64.b64decode(result, validate=True))) as output:
                    self.assertEqual(output.format, 'PNG' if mode == 'RGBA' else 'JPEG')
                    self.assertEqual(output.size, (512, 256))
                    self.assertNotIn('exif', output.info)

    def test_rejects_html_svg_corruption_and_excess_pixels(self):
        raw = BytesIO()
        Image.new('1', (9000, 1)).save(raw, 'PNG')
        for body in (b'<html>denied</html>', b'<svg/>', b'\xff\xd8broken', raw.getvalue()):
            with self.subTest(body=body[:15]):
                with patch.object(images, 'read_public', return_value=(body, PHOTO)):
                    with self.assertRaises(ValueError):
                        images.prepare_image(None, PHOTO, article_url=ARTICLE,
                                             permissions=PERMISSIONS, timeout=(2, 3))

    def test_requires_permission_for_the_exact_image(self):
        with patch.object(images, 'read_public') as fetch:
            with self.assertRaises(ValueError):
                images.prepare_image(None, PHOTO, article_url=ARTICLE,
                                     permissions={ARTICLE: PHOTO + '?different=1'}, timeout=(2, 3))
        fetch.assert_not_called()

    def test_historical_metadata_must_match_authorized_image(self):
        for candidate, expected in ((PHOTO, PHOTO), (PHOTO + '?different=1', None)):
            page = f'<meta property="og:image" content="{candidate}">'.encode()
            with patch.object(images, 'read_public', return_value=(page, ARTICLE)):
                result = images.image_reference(None, ARTICLE, permissions=PERMISSIONS, timeout=(2, 3))
            self.assertEqual(result, expected)


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
