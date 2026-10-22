from types import SimpleNamespace

from settings.service import SettingsService
from subscriptions.service import Subscriptions


def test_flag_metadata_keeps_all_subscription_uris_on_separate_lines(monkeypatch):
    monkeypatch.setattr(SettingsService, '_settings', SimpleNamespace(sub_update_interval='5m'))
    uris = [f'olcrtc://jitsi?datachannel@room-{i}#key-{i}$🇩🇪 DE · vm{i}'
            for i in (1, 2, 3)]
    text = Subscriptions.prepare_sub_text(uris, 'vpn@example.org')
    assert [line for line in text.splitlines() if line.startswith('olcrtc://')] == uris
    assert all('olcrtc://' not in line for line in text.splitlines()
               if line.startswith('##icon:'))
