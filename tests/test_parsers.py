import pytest

from app.adb import AdbCommandError, build_sms_fingerprint, parse_isub_output, parse_sms_query_output


def test_parse_isub_output_counts_embedded_and_active_once() -> None:
    output = """
    SubscriptionController:
     ActiveSubInfoList:
      {id=7 simSlotIndex=1 displayName=giffgaff sws carrierName=CHN-UNICOM — giffgaff isEmbedded=true}
    ++++++++++++++++++++++++++++++++
     AllSubInfoList:
      {id=5 simSlotIndex=-1 displayName=Club carrierName=没有服务 isEmbedded=true}
      {id=6 simSlotIndex=-1 displayName=giffgaff_gh carrierName=没有服务 isEmbedded=true}
      {id=7 simSlotIndex=1 displayName=giffgaff sws carrierName=CHN-UNICOM — giffgaff isEmbedded=true}
      {id=9 simSlotIndex=0 displayName=CMCC carrierName=CMCC isEmbedded=false}
    ++++++++++++++++++++++++++++++++
    """

    snapshot = parse_isub_output(output)

    assert snapshot.embedded_total_count == 3
    assert snapshot.embedded_active_count == 1
    assert [item.sub_id for item in snapshot.subscriptions] == ["7", "9", "5", "6"]
    assert next(item for item in snapshot.subscriptions if item.sub_id == "7").is_active is True


def test_parse_isub_output_keeps_physical_and_esim_active_together() -> None:
    output = """
    SubscriptionController:
     ActiveSubInfoList:
      {id=7 simSlotIndex=1 displayName=giffgaff sws carrierName=CHN-UNICOM — giffgaff isEmbedded=true}
      {id=9 simSlotIndex=0 displayName=CMCC carrierName=CMCC isEmbedded=false}
    ++++++++++++++++++++++++++++++++
     AllSubInfoList:
      {id=5 simSlotIndex=-1 displayName=Club carrierName=没有服务 isEmbedded=true}
      {id=7 simSlotIndex=1 displayName=giffgaff sws carrierName=CHN-UNICOM — giffgaff isEmbedded=true}
      {id=9 simSlotIndex=0 displayName=CMCC carrierName=CMCC isEmbedded=false}
    ++++++++++++++++++++++++++++++++
    """

    snapshot = parse_isub_output(output)

    assert snapshot.embedded_total_count == 2
    assert snapshot.embedded_active_count == 1
    assert [item.sub_id for item in snapshot.subscriptions if item.is_active] == ["7", "9"]
    assert next(item for item in snapshot.subscriptions if item.sub_id == "7").is_active is True
    assert next(item for item in snapshot.subscriptions if item.sub_id == "9").is_active is True


def test_parse_sms_query_output_handles_commas_and_newlines() -> None:
    output = """Row: 0 address=10010, body=第一行,\n第二行继续, sub_id=7, _id=123, date=1716680000000
Row: 1 address=CMCC, body=普通短信, sub_id=5, _id=124, date=1716670000000
"""

    messages = parse_sms_query_output(output)

    assert len(messages) == 2
    assert messages[0].sms_id == "123"
    assert messages[0].body == "第一行,\n第二行继续"
    assert messages[1].address == "CMCC"


def test_parse_sms_query_output_falls_back_when_id_is_missing() -> None:
    output = "Row: 0 address=10086, body=test, sub_id=7, date=1716680000000"

    messages = parse_sms_query_output(output)

    assert len(messages) == 1
    assert messages[0].sms_id == build_sms_fingerprint("10086", "test", "7", "1716680000000")


def test_parse_sms_query_output_rejects_adb_usage_output() -> None:
    output = """usage: adb shell content query --uri <URI> [--projection <PROJECTION>]

[ERROR] Unsupported argument: DESC
"""

    with pytest.raises(AdbCommandError):
        parse_sms_query_output(output)
