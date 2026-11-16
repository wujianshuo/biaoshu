from hashlib import sha256
from importlib.util import find_spec
from io import BytesIO
import json
import socket
import unittest
from unittest.mock import patch

from biaoshu.config import BusinessError
from biaoshu.object_s3 import S3ObjectFiles


class Response(BytesIO):
    def __init__(self, status, body=b""):
        super().__init__(body)
        self.status = status


def address(ip):
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 9000))


class S3ObjectFilesTest(unittest.TestCase):
    def setUp(self):
        self.storage = S3ObjectFiles("http://minio.test:9000", "tenders", "exampleKey", "example-test-secret")
        for name in ("socket.getaddrinfo", "http.client.HTTPConnection", "socket.create_connection"):
            patcher = patch(f"biaoshu.object_s3.{name}")
            mock = patcher.start()
            self.addCleanup(patcher.stop)
            setattr(self, name.split(".")[-1], mock)
        self.getaddrinfo.return_value = [address("192.168.2.20")]
        self.connection = self.HTTPConnection.return_value

    def fake_signature(self):
        signer = patch.object(self.storage, "_signed_headers",
                              side_effect=lambda _method, _path, _body, headers: dict(headers, Authorization="fixture"))
        signer.start()
        self.addCleanup(signer.stop)

    def test_private_bucket_probe_authenticates_and_checks_anonymous_access(self):
        self.fake_signature()
        self.connection.getresponse.side_effect = [Response(200), Response(403), Response(404, b"<Error><Code>NoSuchBucketPolicy</Code></Error>")]
        self.storage.probe()
        requests = self.connection.request.call_args_list
        self.assertEqual([req.args for req in requests], [("HEAD", "/tenders"), ("HEAD", "/tenders"), ("GET", "/tenders?policy")])
        self.assertNotIn("Authorization", requests[1].kwargs["headers"])
        self.assertIn("Authorization", requests[2].kwargs["headers"])
        self.connection._create_connection(("minio.test", 9000), 30, None)
        self.create_connection.assert_called_once_with(("192.168.2.20", 9000), 30, None)

    def test_probe_rejects_public_object_only_policy(self):
        self.fake_signature()
        policy = {"Statement": [{"Effect": "Allow", "Principal": {"AWS": ["*"]}, "Action": "s3:GetObject"}]}
        self.connection.getresponse.side_effect = [Response(200), Response(403), Response(200, json.dumps(policy).encode())]
        with self.assertRaisesRegex(BusinessError, "明确的访问主体"):
            self.storage.probe()

    def test_probe_rejects_anonymous_listing(self):
        self.fake_signature()
        self.connection.getresponse.side_effect = [Response(200), Response(200)]
        with self.assertRaisesRegex(BusinessError, "限制匿名访问"):
            self.storage.probe()
        self.assertEqual(self.connection.request.call_count, 2)

    def test_no_network_on_mixed_private_public_dns(self):
        self.getaddrinfo.return_value = [address("192.168.2.20"), address("8.8.8.8")]
        with self.assertRaisesRegex(BusinessError, "全部解析"):
            self.storage.probe()
        self.HTTPConnection.assert_not_called()

    def test_object_is_namespaced_and_sha256_verified(self):
        self.fake_signature()
        payload = "技术标附件".encode()
        wanted = sha256(payload).hexdigest()
        self.connection.getresponse.side_effect = [Response(200), Response(200), Response(200, payload), Response(200, b"damaged")]
        self.assertEqual(self.storage.write("tenant-1", "project-a", payload), wanted)
        self.assertEqual(self.storage.write("tenant-2", "project-a", payload), wanted)
        self.assertEqual(self.storage.read("tenant-1", "project-a", wanted), payload)
        paths = [req.args[1] for req in self.connection.request.call_args_list]
        self.assertEqual(paths[0], paths[2])
        self.assertNotEqual(paths[0], paths[1])
        self.assertTrue(paths[0].endswith(f"/{wanted[:2]}/{wanted}"))
        self.assertNotIn("tenant-1", paths[0])
        with self.assertRaisesRegex(BusinessError, "摘要与档案不一致"):
            self.storage.read("tenant-1", "project-a", wanted)

    def test_does_not_follow_object_redirect(self):
        self.fake_signature()
        self.connection.getresponse.return_value = Response(301, b"must not be followed")
        with self.assertRaisesRegex(BusinessError, "对象存储读取失败"):
            self.storage.read("tenant", "project", "0" * 64)
        self.assertEqual(self.connection.request.call_count, 1)

class S3SignerTest(unittest.TestCase):
    @unittest.skipUnless(find_spec("botocore"), "Botocore not installed")
    def test_upload_signs_real_payload_and_preserves_host(self):
        storage = S3ObjectFiles("http://minio.test:9000", "tenders", "exampleKey", "example-test-secret")
        payload = b"sign this payload"
        wanted = sha256(payload).hexdigest()
        path = storage._path("tenant", "project", wanted)
        headers = storage._signed_headers("PUT", path, payload, {"Host": "minio.test:9000"})
        self.assertTrue(headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=exampleKey/"))
        self.assertIn("/us-east-1/s3/aws4_request", headers["Authorization"])
        self.assertEqual(headers["X-Amz-Content-SHA256"], wanted)
        self.assertEqual(headers["Host"], "minio.test:9000")
