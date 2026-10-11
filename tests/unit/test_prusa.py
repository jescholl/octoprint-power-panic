from octoprint_power_panic import prusa


def test_firmware_version():
    assert prusa.firmware_version("Prusa-Firmware 3.14.1 based on Marlin") == (3, 14, 1)
    assert prusa.firmware_version("Prusa-Firmware 3.14.1+8237_74a577bc0") == (3, 14, 1)
    assert prusa.firmware_version("Marlin 2.1.2.4") is None
    assert prusa.firmware_version(None) is None


def test_supports_host_recovery():
    assert prusa.supports_host_recovery((3, 14, 0))
    assert prusa.supports_host_recovery((3, 15, 2))
    assert not prusa.supports_host_recovery((3, 13, 3))
    assert not prusa.supports_host_recovery(None)


def test_parse_line_number_little_endian():
    # 0x0000312a = 12586
    assert prusa.parse_line_number("0f91  2a 31 00 00") == 12586
    assert prusa.parse_line_number("000f91 01 00 00 00\n") == 1


def test_parse_line_number_rejects_other_lines():
    assert prusa.parse_line_number("0f92  2a 31 00 00") is None
    assert prusa.parse_line_number("ok") is None
    assert prusa.parse_line_number("D3 - Read/Write EEPROM") is None
    assert prusa.parse_line_number("0f91  2a 31 00") is None
