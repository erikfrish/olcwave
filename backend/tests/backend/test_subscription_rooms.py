"""Issued Jitsi subscriptions must not place independent user servers in one room."""
import pytest

from test_rw_authorization import (
    allow, fake_access, fake_docker, no_db, ok_traffic, panel_env,
)


@pytest.mark.asyncio
@pytest.mark.parametrize('room', [
    'meet.example.org/shared-room',
    'https://meet.example.org/shared-room',
    'https://meet.example.org',
])
async def test_subscriptions_isolate_user_rooms(
    room, fake_access, fake_docker, no_db, panel_env,
):
    from profiles.schemas import ProfileSchema
    from subscriptions.service import Subscriptions
    import yaml
    from urllib.parse import urlsplit

    panel_env.profiles.append(ProfileSchema(
        name='Jitsi', tag='jitsi', profile=yaml.safe_dump({
            'auth': {'provider': 'jitsi'},
            'room': {'id': room},
            'net': {'transport': 'datachannel'},
        }),
    ))
    issued = []
    for user in ('alice', 'bob'):
        panel_env.traffic[user] = ok_traffic(user)
        fake_access.decision = allow(user)
        response = await Subscriptions.get(user)
        assert response.status_code == 200
        config = yaml.safe_load(panel_env.configs[f'olcwave-jitsi-{user}'])
        parsed = urlsplit(config['room']['id'])
        assert parsed.scheme == 'https'
        assert parsed.netloc == 'meet.example.org'
        assert parsed.path.strip('/')
        assert not parsed.query and not parsed.fragment
        assert config['room']['id'] in response.body.decode()
        issued.append(config)

    assert issued[0]['room']['id'] != issued[1]['room']['id']
    assert issued[0]['crypto']['key'] != issued[1]['crypto']['key']


@pytest.mark.asyncio
async def test_trial_profiles_only_reach_selected_subscribers(
    fake_access, fake_docker, no_db, panel_env, monkeypatch,
):
    import json
    import yaml
    from config import settings
    from profiles.schemas import ProfileSchema
    from subscriptions.service import Subscriptions

    monkeypatch.setattr(settings, 'OLCWAVE_PROFILE_USERS_JSON', json.dumps({'trial': ['alice']}))
    for tag in ('ordinary', 'trial'):
        panel_env.profiles.append(ProfileSchema(name=tag, tag=tag, profile=yaml.safe_dump({
            'auth': {'provider': 'salutejazz'}, 'room': {'id': 'room:password'},
            'net': {'transport': 'datachannel'},
        })))
    for user in ('alice', 'bob'):
        panel_env.traffic[user] = ok_traffic(user)
        fake_access.decision = allow(user)
        response = await Subscriptions.get(user)
        assert response.status_code == 200
        body = response.body.decode()
        assert '$ordinary' in body
        assert ('$trial' in body) == (user == 'alice')
    assert 'olcwave-trial-alice' in panel_env.configs
    assert 'olcwave-trial-bob' not in panel_env.configs

    configs, profiles = await Subscriptions.get_existing('bob')
    assert set(profiles) == {'ordinary'}
