#!/usr/bin/env python3
"""房間號編碼測試。  python3 test_roomcode.py"""
import ipaddress
import random
import unittest

import roomcode


class RoomCodeTest(unittest.TestCase):
    def test_roundtrip_and_length(self):
        cases = {"192.168.1.23": 5, "192.168.255.255": 5, "172.16.0.1": 5, "172.31.9.9": 5,
                 "10.0.0.1": 6, "10.255.3.200": 6, "140.117.1.5": 8, "127.0.0.1": 8}
        for ip, n in cases.items():
            code = roomcode.encode(ip)
            self.assertEqual(len(code), n, code)
            self.assertEqual(roomcode.decode(code), (ip, 9100, "lobby"))
        rnd = random.Random(1)
        for _ in range(3000):                         # 隨機 IP 全部都能正確還原
            ip = str(ipaddress.IPv4Address(rnd.getrandbits(32)))
            self.assertEqual(roomcode.decode(roomcode.encode(ip))[0], ip)

    def test_typing_mistakes_tolerated(self):
        code = roomcode.encode("192.168.0.33")        # A0011
        self.assertEqual(code, "A0011")
        for typed in ("a0011", " A0 011 ", "AOOII", "a0ol1", "Aoo11"):
            self.assertEqual(roomcode.decode(typed)[0], "192.168.0.33", typed)

    def test_port_and_room(self):
        code = roomcode.encode("192.168.1.23", 9200, "design-2")
        self.assertEqual(code, "A008Q:9200-design-2")
        self.assertEqual(roomcode.decode(code), ("192.168.1.23", 9200, "design-2"))
        self.assertEqual(roomcode.decode("a008q-Art"), ("192.168.1.23", 9100, "Art"))

    def test_invalid(self):
        for bad in ("", "A00", "A00111", "E1234", "A00U1", "A008Q:99999", "A0?11", "-room",
                    "B" + "Z" * 4):                   # B 只有 20 位元，ZZZZ 是合法上限
            if bad == "B" + "Z" * 4:
                self.assertEqual(roomcode.decode(bad)[0], "172.31.255.255")
                continue
            with self.assertRaises(ValueError, msg=bad):
                roomcode.decode(bad)
        with self.assertRaises(ValueError):
            roomcode.decode("D" + "Z" * 7)            # 超過 32 位元


if __name__ == "__main__":
    unittest.main(verbosity=2)
