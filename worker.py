from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from functools import partial
from geopy.distance import great_circle
from logging import getLogger
from pgoapi import exceptions, PGoApi
from pgoapi.auth_ptc import AuthPtc
from pgoapi.utilities import get_cell_ids, f2i
from asyncio import sleep, Lock
from random import choice, randint, uniform, triangular
from time import time, monotonic

from db import SIGHTING_CACHE, MYSTERY_CACHE
from utils import random_sleep, round_coords, load_pickle, load_accounts, get_device_info, get_spawn_id, get_distance
from shared import DatabaseProcessor

import config

if config.NOTIFY:
    from notification import Notifier

if config.CONTROL_SOCKS:
    from stem import Signal
    from stem.control import Controller
    import stem.util.log
    stem.util.log.get_logger().level = 40
    CIRCUIT_TIME = dict()
    CIRCUIT_FAILURES = dict()
    for proxy in config.PROXIES:
        CIRCUIT_TIME[proxy] = monotonic()
        CIRCUIT_FAILURES[proxy] = 0
else:
    CIRCUIT_TIME = None
    CIRCUIT_FAILURES = None

class Worker:
    """Single worker walking on the map"""

    network_executor = ThreadPoolExecutor(config.NETWORK_THREADS)
    download_hash = "d3da400db60abf79ea05abc38e2396f0bbd453f9"
    g = {'seen': 0, 'captchas': 0}
    db_processor = DatabaseProcessor()
    spawns = db_processor.spawns
    accounts = load_accounts()
    cell_ids = load_pickle('cells') or {}

    if config.NOTIFY:
        notifier = Notifier(spawns)
        g['sent'] = 0

    def __init__(self, worker_no, proxy=None):
        self.worker_no = worker_no
        self.logger = getLogger('worker-{}'.format(worker_no))
        # account information
        self.account = self.extra_queue.get()
        self.username = self.account.get('username')
        self.location = self.account.get('location', (0, 0, 0))
        self.inventory_timestamp = self.account.get('inventory_timestamp')
        self.last_request = self.account.get('time', 0)
        self.last_action = self.last_request
        self.last_gmo = self.last_request
        self.items = self.account.get('items', {})
        # API setup
        self.proxy = proxy
        self.initialize_api()
        # State variables
        self.busy = BusyLock()
        self.killed = False
        # Other variables
        self.after_spawn = None
        self.speed = 0
        self.account_start = None
        self.total_seen = 0
        self.error_code = 'INIT'
        self.item_capacity = 350
        self.visits = 0

    def initialize_api(self):
        device_info = get_device_info(self.account)
        self.logged_in = False
        self.ever_authenticated = False
        self.empty_visits = 0
        self.account_seen = 0

        self.api = PGoApi(device_info=device_info)
        if config.ENCRYPT_PATH:
            self.api.set_signature_lib(config.ENCRYPT_PATH)
        if config.HASH_PATH:
            self.api.set_hash_lib(config.HASH_PATH)
        self.api.set_position(*self.location)
        self.set_proxy()
        self.api.set_logger(self.logger)
        if self.account.get('provider') == 'ptc' and self.account.get('refresh'):
            self.api._auth_provider = AuthPtc()
            self.api._auth_provider.set_refresh_token(self.account.get('refresh'))
            self.api._auth_provider._access_token = self.account.get('auth')
            self.api._auth_provider._access_token_expiry = self.account.get('expiry')
            if self.api._auth_provider.check_access_token():
                self.api._auth_provider._login = True
                self.logged_in = True
                self.ever_authenticated = True

    def set_proxy(self, proxy=None):
        if proxy:
            self.proxy = proxy
        if self.proxy:
            self.api.set_proxy({'http': proxy, 'https': proxy})

    def swap_proxy(self, reason=''):
        self.set_proxy(choice(config.PROXIES))
        self.logger.warning('Swapped out {p} due to {r}.'.format(
                            p=self.proxy, r=reason))

    def swap_circuit(self, reason=''):
        if not config.CONTROL_SOCKS:
            if config.PROXIES:
                self.swap_proxy(reason=reason)
            return
        time_passed = monotonic() - CIRCUIT_TIME[self.proxy]
        if time_passed > 180:
            socket = config.CONTROL_SOCKS[self.proxy]
            with Controller.from_socket_file(path=socket) as controller:
                controller.authenticate()
                controller.signal(Signal.NEWNYM)
            CIRCUIT_TIME[self.proxy] = monotonic()
            CIRCUIT_FAILURES[self.proxy] = 0
            self.logger.warning('Changed circuit on {p} due to {r}.'.format(
                                p=self.proxy, r=reason))
        else:
            self.logger.info('Skipped changing circuit on {p} because it was '
                             'changed {s} seconds ago.'.format(
                                 p=self.proxy, s=time_passed))

    async def login(self):
        """Logs worker in and prepares for scanning"""
        self.logger.info('Trying to log in')
        self.error_code = 'LOGIN'

        async with self.login_semaphore:
            await random_sleep(minimum=0.5, maximum=1.5)
            await self.loop.run_in_executor(
                self.network_executor,
                partial(
                    self.api.set_authentication,
                    username=self.username,
                    password=self.account.get('password'),
                    provider=self.account.get('provider'),
                )
            )
        if self.killed:
            return False
        if not self.ever_authenticated:
            async with self.simulation_semaphore:
                if not await self.app_simulation_login():
                    return False

        self.ever_authenticated = True
        self.logged_in = True
        self.error_code = None
        self.account_start = time()
        return True

    async def app_simulation_login(self):
        self.error_code = 'APP SIMULATION'
        self.logger.info('Starting RPC login sequence (iOS app simulation)')

        # empty request 1
        request = self.api.create_request()
        await self.loop.run_in_executor(self.network_executor, request.call)
        await random_sleep(0.3, 0.5)

        # empty request 2
        request = self.api.create_request()
        await self.loop.run_in_executor(self.network_executor, request.call)
        await random_sleep(0.2, 0.4)

        # request 1: get_player
        request = self.api.create_request()
        request.get_player(
            player_locale={
                'country': 'US',
                'language': 'en',
                'timezone': 'America/Denver'})

        response = await self.loop.run_in_executor(
            self.network_executor, request.call
        )

        get_player = response.get('responses', {}).get('GET_PLAYER', {})
        tutorial_state = get_player.get('player_data', {}).get('tutorial_state', [])
        self.item_capacity = get_player.get('player_data', {}).get('max_item_storage', 350)

        if get_player.get('banned', False):
            raise exceptions.BannedAccountException
            return False

        await random_sleep(.9, 1.2)

        version = 5102
        # request 2: download_remote_config_version
        request = self.api.create_request()
        request.download_remote_config_version(platform=1, app_version=version)
        responses = await self.call_chain(request, stamp=False, buddy=False, dl_hash=False)

        inventory_items = responses.get('GET_INVENTORY', {}).get('inventory_delta', {}).get('inventory_items', [])
        player_level = None
        for item in inventory_items:
            player_stats = item.get('inventory_item_data', {}).get('player_stats', {})
            if player_stats:
                player_level = player_stats.get('level')
                break

        await random_sleep(.5, .7)

        # request 3: get_asset_digest
        request = self.api.create_request()
        request.get_asset_digest(platform=1, app_version=version)
        await self.call_chain(request, buddy=False)

        await random_sleep(1.2, 1.4)

        if (config.COMPLETE_TUTORIAL and
                tutorial_state is not None and
                not all(x in tutorial_state for x in (0, 1, 3, 4, 7))):
            self.logger.warning('Starting tutorial')
            await self.complete_tutorial(tutorial_state)
        else:
            # request 4: get_player_profile
            request = self.api.create_request()
            request.get_player_profile()
            await self.call_chain(request)
            await random_sleep(.2, .4)

        if player_level:
            # request 5: level_up_rewards
            request = self.api.create_request()
            request.level_up_rewards(level=player_level)
            await self.call_chain(request)
            await random_sleep(.9, 1.1)
        else:
            self.logger.warning('No player level')

        request = self.api.create_request()
        request.register_background_device(device_type='apple_watch')
        await self.call_chain(request)
        self.last_action = time()
        await random_sleep(.1, .3)

        self.logger.info('Finished RPC login sequence (iOS app simulation)')
        self.error_code = None
        return True

    async def complete_tutorial(self, tutorial_state):
        self.error_code = 'TUTORIAL'
        if 0 not in tutorial_state:
            await random_sleep(1, 5)
            request = self.api.create_request()
            request.mark_tutorial_complete(tutorials_completed=0)
            await self.call_chain(request, buddy=False)

        if 1 not in tutorial_state:
            await random_sleep(5, 12)
            request = self.api.create_request()
            request.set_avatar(player_avatar={
                    'hair': randint(1,5),
                    'shirt': randint(1,3),
                    'pants': randint(1,2),
                    'shoes': randint(1,6),
                    'gender': randint(0,1),
                    'eyes': randint(1,4),
                    'backpack': randint(1,5)
                })
            await self.call_chain(request, buddy=False)

            await random_sleep(.3, .5)

            request = self.api.create_request()
            request.mark_tutorial_complete(tutorials_completed=1)
            await self.call_chain(request, buddy=False)

        await random_sleep(.5, .6)
        request = self.api.create_request()
        request.get_player_profile()
        await self.call_chain(request)

        starter_id = None
        if 3 not in tutorial_state:
            await random_sleep(1, 1.5)
            request = self.api.create_request()
            request.get_download_urls(asset_id=['1a3c2816-65fa-4b97-90eb-0b301c064b7a/1477084786906000',
                                                'aa8f7687-a022-4773-b900-3a8c170e9aea/1477084794890000',
                                                'e89109b0-9a54-40fe-8431-12f7826c8194/1477084802881000'])
            await self.call_chain(request)

            await random_sleep(1, 1.6)
            request = self.api.create_request()
            await self.loop.run_in_executor(self.network_executor, request.call)

            await random_sleep(6, 13)
            request = self.api.create_request()
            starter = choice((1, 4, 7))
            request.encounter_tutorial_complete(pokemon_id=starter)
            await self.call_chain(request)

            await random_sleep(.5, .6)
            request = self.api.create_request()
            request.get_player(
                player_locale={
                    'country': 'US',
                    'language': 'en',
                    'timezone': 'America/Denver'})
            responses = await self.call_chain(request)

            inventory = responses.get('GET_INVENTORY', {}).get('inventory_delta', {}).get('inventory_items', [])
            for item in inventory:
                pokemon = item.get('inventory_item_data', {}).get('pokemon_data')
                if pokemon:
                    starter_id = pokemon.get('id')


        if 4 not in tutorial_state:
            await random_sleep(5, 12)
            request = self.api.create_request()
            request.claim_codename(codename=self.username)
            await self.call_chain(request)

            await random_sleep(1, 1.3)
            request = self.api.create_request()
            request.mark_tutorial_complete(tutorials_completed=4)
            await self.call_chain(request, buddy=False)

            await sleep(.1)
            request = self.api.create_request()
            request.get_player(
                player_locale={
                    'country': 'US',
                    'language': 'en',
                    'timezone': 'America/Denver'})
            await self.call_chain(request)

        if 7 not in tutorial_state:
            await random_sleep(4, 10)
            request = self.api.create_request()
            request.mark_tutorial_complete(tutorials_completed=7)
            await self.call_chain(request)

        if starter_id:
            await random_sleep(3, 5)
            request = self.api.create_request()
            request.set_buddy_pokemon(pokemon_id=starter_id)
            await random_sleep(.8, 1.8)

        await sleep(.2)
        return True

    def update_inventory(self, inventory_items):
        for thing in inventory_items:
            item = thing.get('inventory_item_data', {}).get('item')
            if not item:
                continue
            item_id = item.get('item_id')
            self.items[item_id] = item.get('count', 0)

    async def call_chain(self, request, stamp=True, buddy=True, dl_hash=True):
        request.check_challenge()
        request.get_hatched_eggs()
        if stamp and self.inventory_timestamp:
            request.get_inventory(last_timestamp_ms=self.inventory_timestamp)
        else:
            request.get_inventory()
        request.check_awarded_badges()
        if dl_hash:
            request.download_settings(hash=self.download_hash)
        else:
            request.download_settings()
        if buddy:
            request.get_buddy_walked()

        response = await self.loop.run_in_executor(
            self.network_executor, request.call
        )
        self.last_request = time()
        try:
            if response.get('status_code') == 3:
                raise exceptions.BannedAccountException
            responses = response.get('responses')
            delta = responses.get('GET_INVENTORY', {}).get('inventory_delta', {})
            timestamp = delta.get('new_timestamp_ms')
            inventory_items = delta.get('inventory_items', [])
            if inventory_items:
                self.update_inventory(inventory_items)
            self.inventory_timestamp = timestamp or self.inventory_timestamp
            d_hash = responses.get('DOWNLOAD_SETTINGS', {}).get('hash')
            self.download_hash = d_hash or self.download_hash
            self.check_captcha(responses)
        except (TypeError, AttributeError):
            raise MalformedResponse
        return responses

    def fast_speed(self, point):
        '''Fast but inaccurate estimation of travel speed to point'''
        if self.busy.locked() or self.killed:
            return None
        now = time()
        if now - self.last_gmo < config.SCAN_DELAY:
            return None
        time_diff = now - self.last_request
        if time_diff > 60:
            self.error_code = None
        distance = get_distance(self.location, point)
        # rough conversion from degrees/second to miles/hour
        speed = (distance / time_diff) * 223694
        return speed

    def accurate_speed(self, point):
        '''Slow but accurate estimation of travel speed to point'''
        time_diff = time() - self.last_request
        distance = great_circle(self.location, point).miles
        speed = (distance / time_diff) * 3600
        return speed

    async def visit(self, point, bootstrap=False):
        """Wrapper for self.visit_point - runs it a few times before giving up

        Also is capable of restarting in case an error occurs.
        """
        visited = False
        for attempts in range(0, 4):
            try:
                if not self.logged_in:
                    self.api.set_position(*point)
                    if not await self.login():
                        await sleep(2)
                        continue
                visited = await self.visit_point(point, bootstrap=bootstrap)
            except exceptions.ServerSideAccessForbiddenException:
                err = 'Banned IP.'
                if self.proxy:
                    err += ' ' + self.proxy
                self.logger.error(err)
                self.error_code = 'IP BANNED'
                self.swap_circuit(reason='ban')
                await random_sleep(minimum=25, maximum=35)
            except exceptions.AuthException:
                self.logger.warning('Login failed: {}'.format(self.username))
                self.error_code = 'FAILED LOGIN'
                await self.swap_account(reason='login failed')
                return False
            except exceptions.NotLoggedInException:
                self.logger.error('{} is not logged in.'.format(self.username))
                self.error_code = 'NOT AUTHENTICATED'
                await self.swap_account(reason='not logged in')
                return False
            except exceptions.ServerBusyOrOfflineException:
                self.logger.info('Server too busy - restarting')
                self.error_code = 'RETRYING'
                await random_sleep()
            except exceptions.ServerSideRequestThrottlingException:
                self.logger.warning('Server throttling - sleeping for a bit')
                self.error_code = 'THROTTLE'
                await random_sleep(11, 30)
            except exceptions.BannedAccountException:
                self.error_code = 'BANNED?'
                self.logger.warning('Account appears to be banned')
                await self.remove_account()
                return False
            except CaptchaException:
                self.error_code = 'CAPTCHA'
                await self.bench_account()
                self.g['captchas'] += 1
                return False
            except MalformedResponse:
                self.logger.warning('Malformed response received!')
                self.error_code = 'MALFORMED RESPONSE'
                await random_sleep()
            except Exception as err:
                self.logger.exception('A wild exception appeared! {}'.format(err))
                self.error_code = 'EXCEPTION'
                return False
            else:
                return visited
        return False

    async def visit_point(self, point, bootstrap=False):
        latitude, longitude = point
        rounded = round_coords(point, precision=3)
        altitude = self.spawns.get_altitude(rounded)
        altitude = uniform(altitude - 1, altitude + 1)
        self.error_code = '!'
        self.logger.info(
            'Visiting {0[0]:.4f},{0[1]:.4f}'.format(point))
        start = time()
        self.location = point + [altitude]

        self.api.set_position(latitude, longitude, altitude)

        rounded = round_coords(point, precision=4)
        if rounded in self.cell_ids:
            cell_ids = list(self.cell_ids[rounded])
        else:
            cell_ids = get_cell_ids(*rounded, radius=500)
            self.cell_ids[rounded] = tuple(cell_ids)

        since_timestamp_ms = [0] * len(cell_ids)

        request = self.api.create_request()
        request.get_map_objects(cell_id=cell_ids,
                                since_timestamp_ms=since_timestamp_ms,
                                latitude=f2i(latitude),
                                longitude=f2i(longitude))

        responses = await self.call_chain(request)
        self.last_gmo = time()

        map_objects = responses.get('GET_MAP_OBJECTS', {})

        sent = False
        pokemon_seen = 0
        forts_seen = 0

        if map_objects.get('status') != 1:
            self.error_code = 'UNKNOWNRESPONSE'
            self.logger.warning(
                'Response code: {}'.format(map_objects.get('status')))
            self.empty_visits += 1
            if self.empty_visits > 2:
                reason = '{} empty visits'.format(self.empty_visits)
                await self.swap_account(reason)
            return False
        for map_cell in map_objects['map_cells']:
            request_time_ms = map_cell['current_timestamp_ms']
            for pokemon in map_cell.get('wild_pokemons', []):
                pokemon_seen += 1
                # Accurate times only provided in the last 90 seconds
                invalid_tth = (
                    pokemon['time_till_hidden_ms'] < 0 or
                    pokemon['time_till_hidden_ms'] > 90000
                )
                normalized = self.normalize_pokemon(
                    pokemon,
                    request_time_ms
                )
                if invalid_tth:
                    despawn_time = self.spawns.get_despawn_time(
                        normalized['spawn_id'], normalized['seen'])
                    if despawn_time:
                        normalized['expire_timestamp'] = despawn_time
                        normalized['time_till_hidden_ms'] = (
                            despawn_time * 1000) - request_time_ms
                        normalized['valid'] = 'fixed'
                    else:
                        normalized['valid'] = False
                else:
                    normalized['valid'] = True

                normalized, sent = await self.notify(normalized, pokemon)

                if (normalized not in SIGHTING_CACHE and
                        normalized not in MYSTERY_CACHE):
                    self.account_seen += 1
                    self.db_processor.add(normalized)
                    if (config.ENCOUNTER == 'all' and
                            'individual_attack' not in normalized):
                        try:
                            normalized.update(await self.encounter(pokemon))
                        except Exception:
                            self.logger.warning('Exception during encounter.')
            for fort in map_cell.get('forts', []):
                if not fort.get('enabled'):
                    continue
                forts_seen += 1
                if fort.get('type') == 1:  # pokestops
                    if 'lure_info' in fort:
                        norm = self.normalize_lured(fort, request_time_ms)
                        pokemon_seen += 1
                        if norm not in SIGHTING_CACHE:
                            self.account_seen += 1
                            self.db_processor.add(norm)
                    pokestop = self.normalize_pokestop(fort)
                    self.db_processor.add(pokestop)
                    if (config.SPIN_POKESTOPS and
                            sum(self.items.values()) < self.item_capacity):
                        cooldown = fort.get('cooldown_complete_timestamp_ms')
                        if not cooldown or time() > cooldown / 1000:
                            await self.spin_pokestop(pokestop)
                else:
                    self.db_processor.add(self.normalize_gym(fort))
            if bootstrap:
                for point in map_cell.get('spawn_points', []):
                    point = (point.get('latitude'), point.get('longitude'))
                    try:
                        rounded = round_coords(point, precision=4)
                    except TypeError:
                        pass
                    else:
                        self.spawns.mysteries.add(rounded)

        if pokemon_seen > 0:
            self.error_code = ':'
            self.total_seen += pokemon_seen
            self.g['seen'] += pokemon_seen
            self.empty_visits = 0
            if CIRCUIT_FAILURES:
                CIRCUIT_FAILURES[self.proxy] = 0
        else:
            self.empty_visits += 1
            if forts_seen == 0:
                self.error_code = 'NOTHING SEEN'
                await self.swap_account('no Pokemon or forts seen')
            else:
                self.error_code = ','
            if self.empty_visits > 2:
                reason = '{} empty visits'.format(self.empty_visits)
                await self.swap_account(reason)
            if CIRCUIT_FAILURES:
                CIRCUIT_FAILURES[self.proxy] += 1
                if CIRCUIT_FAILURES[self.proxy] > 20:
                    reason = '{} empty visits'.format(
                        CIRCUIT_FAILURES[self.proxy])
                    self.swap_circuit(reason)

        self.visits += 1
        if config.MAP_WORKERS:
            self.worker_dict.update([(self.worker_no,
                ((latitude, longitude), start, self.speed, self.total_seen,
                self.visits, pokemon_seen, sent))])
        self.logger.info(
            'Point processed, %d Pokemon and %d forts seen!',
            pokemon_seen,
            forts_seen,
        )
        self.update_accounts_dict()
        return True

    async def spin_pokestop(self, pokestop):
        self.error_code = '$'
        pokestop_location = pokestop['lat'], pokestop['lon']
        distance = great_circle(self.location, pokestop_location).meters
        if distance > 40:
            return False

        if time() - self.last_action < 1:
            await sleep(1)

        request = self.api.create_request()
        request.fort_details(fort_id = pokestop['external_id'],
                             latitude = pokestop['lat'],
                             longitude = pokestop['lon'])
        responses = await self.call_chain(request)
        name = responses.get('FORT_DETAILS', {}).get('name')

        await random_sleep(.6, 1.2)

        request = self.api.create_request()
        request.fort_search(fort_id = pokestop['external_id'],
                            player_latitude = self.location[0],
                            player_longitude = self.location[1],
                            fort_latitude = pokestop['lat'],
                            fort_longitude = pokestop['lon'])
        responses = await self.call_chain(request)
        self.last_action = time() + .25

        result = responses.get('FORT_SEARCH', {}).get('result')
        if result == 1:
            self.logger.info('Spun {n}: {r}'.format(n=name, r=result))
        else:
            self.logger.warning('Failed spinning {n}: {r}'.format(n=name, r=result))
        self.error_code = '!'
        return responses

    async def encounter(self, pokemon):
        pokemon_point = pokemon['latitude'], pokemon['longitude']
        distance_to_pokemon = great_circle(self.location, pokemon_point).meters

        if distance_to_pokemon > 47:
            percent = 1 - (46 / distance_to_pokemon)
            lat_change = (self.location[0] - pokemon['latitude']) * percent
            lon_change = (self.location[1] - pokemon['longitude']) * percent
            self.location = [
                self.location[0] - lat_change,
                self.location[1] - lon_change,
                uniform(self.location[2] - 3, self.location[2] + 3)
            ]
            self.api.set_position(*self.location)
            delay_required = (distance_to_pokemon * percent) / 8
            if delay_required < 1.5:
                delay_required = triangular(1.25, 4, 2)
        else:
            self.simulate_jitter()
            delay_required = triangular(1.25, 4, 2)

        if time() - self.last_request < delay_required:
            self.error_code = '~'
            await sleep(delay_required)

        self.error_code = 'ENCOUNTERING'

        request = self.api.create_request()
        request = request.encounter(encounter_id=pokemon['encounter_id'],
                                    spawn_point_id=pokemon['spawn_point_id'],
                                    player_latitude=self.location[0],
                                    player_longitude=self.location[1])

        responses = await self.call_chain(request)
        self.last_action = time() + 1.5

        response = responses.get('ENCOUNTER', {})
        pokemon_data = response.get('wild_pokemon', {}).get('pokemon_data', {})
        if 'cp' in pokemon_data:
            for iv in ('individual_attack',
                       'individual_defense',
                       'individual_stamina'):
                if iv not in pokemon_data:
                    pokemon_data[iv] = 0
            pokemon_data['probability'] = response.get(
                'capture_probability', {}).get('capture_probability')
        self.error_code = '!'
        return pokemon_data

    def simulate_jitter(self, amount=0.00002):
        self.location = [
            uniform(self.location[0] - amount,
                           self.location[0] + amount),
            uniform(self.location[1] - amount,
                           self.location[1] + amount),
            uniform(self.location[2] - 1.5,
                           self.location[2] + 1.5)
        ]
        self.api.set_position(*self.location)

    async def notify(self, norm, pokemon):
        if config.NOTIFY and self.notifier.eligible(norm):
            if config.ENCOUNTER in ('all', 'notifying'):
                norm.update(await self.encounter(pokemon))
            self.error_code = '*'
            notified, explanation = self.notifier.notify(norm)
            self.logger.info(explanation)
            if notified:
                self.g['sent'] += 1
            self.error_code = '!'
            return norm, notified
        else:
            return norm, False

    def update_accounts_dict(self, captcha=False, banned=False):
        self.account['captcha'] = captcha
        self.account['banned'] = banned
        self.account['location'] = self.location
        self.account['time'] = self.last_request
        self.account['inventory_timestamp'] = self.inventory_timestamp
        self.account['items'] = self.items

        if self.api._auth_provider:
            self.account['refresh'] = self.api._auth_provider._refresh_token
            if self.api._auth_provider.check_access_token():
                self.account['auth'] = self.api._auth_provider._access_token
                self.account['expiry'] = self.api._auth_provider._access_token_expiry
            else:
                self.account['auth'], self.account['expiry'] = None, None

        self.accounts[self.username] = self.account

    async def remove_account(self):
        self.error_code = 'REMOVING'
        self.logger.warning('Removing {} due to ban.'.format(self.username))
        self.update_accounts_dict(banned=True)
        await self.new_account()

    async def bench_account(self):
        self.error_code = 'BENCHING'
        self.logger.warning('Swapping {} due to CAPTCHA.'.format(self.username))
        self.update_accounts_dict(captcha=True)
        self.captcha_queue.put(self.account)
        await self.new_account()

    async def swap_account(self, reason='', lock=False):
        self.error_code = 'SWAPPING'
        self.logger.warning('Swapping out {u} because {r}.'.format(
                            u=self.username, r=reason))
        if lock:
            await self.busy.acquire()
        self.update_accounts_dict()
        while self.extra_queue.empty():
            if self.killed:
                return False
            await sleep(15)
        self.extra_queue.put(self.account)
        await self.new_account(lock)

    async def new_account(self, lock=False):
        while self.extra_queue.empty():
            if self.killed:
                return False
            await sleep(15)
        self.account = self.extra_queue.get()
        self.username = self.account.get('username')
        self.location = self.account.get('location', (0, 0, 0))
        self.inventory_timestamp = self.account.get('inventory_timestamp')
        self.last_request = self.account.get('time', 0)
        self.last_action = self.last_request
        self.last_gmo = self.last_request
        self.items = self.account.get('items', {})
        self.initialize_api()
        self.error_code = None
        if lock:
            self.busy.release()

    def seen_per_second(self, now):
        try:
            seconds_active = now - self.account_start
            if seconds_active < 120:
                return None
            return self.account_seen / seconds_active
        except TypeError:
            return None

    def kill(self):
        """Marks worker as killed

        Killed worker won't be restarted.
        """
        self.error_code = 'KILLED'
        self.killed = True
        if self.ever_authenticated:
            self.update_accounts_dict()

    @staticmethod
    def normalize_pokemon(raw, now):
        """Normalizes data coming from API into something acceptable by db"""
        return {
            'type': 'pokemon',
            'encounter_id': raw['encounter_id'],
            'pokemon_id': raw['pokemon_data']['pokemon_id'],
            'expire_timestamp': round((now + raw['time_till_hidden_ms']) / 1000),
            'lat': raw['latitude'],
            'lon': raw['longitude'],
            'spawn_id': get_spawn_id(raw),
            'time_till_hidden_ms': raw['time_till_hidden_ms'],
            'seen': round(raw['last_modified_timestamp_ms'] / 1000)
        }

    @staticmethod
    def normalize_lured(raw, now):
        return {
            'type': 'pokemon',
            'encounter_id': raw['lure_info']['encounter_id'],
            'pokemon_id': raw['lure_info']['active_pokemon_id'],
            'expire_timestamp': raw['lure_info']['lure_expires_timestamp_ms'] / 1000,
            'lat': raw['latitude'],
            'lon': raw['longitude'],
            'spawn_id': -1,
            'time_till_hidden_ms': raw['lure_info']['lure_expires_timestamp_ms'] - now,
            'valid': 'pokestop'
        }

    @staticmethod
    def normalize_gym(raw):
        return {
            'type': 'fort',
            'external_id': raw['id'],
            'lat': raw['latitude'],
            'lon': raw['longitude'],
            'team': raw.get('owned_by_team', 0),
            'prestige': raw.get('gym_points', 0),
            'guard_pokemon_id': raw.get('guard_pokemon_id', 0),
            'last_modified': round(raw['last_modified_timestamp_ms'] / 1000),
        }

    @staticmethod
    def normalize_pokestop(raw):
        return {
            'type': 'pokestop',
            'external_id': raw['id'],
            'lat': raw['latitude'],
            'lon': raw['longitude']
        }

    @staticmethod
    def check_captcha(responses):
        challenge_url = responses.get('CHECK_CHALLENGE', {}).get('challenge_url', ' ')
        if challenge_url != ' ':
            raise CaptchaException
        else:
            return False

    @property
    def status(self):
        """Returns status message to be displayed in status screen"""
        if self.error_code:
            msg = self.error_code
        else:
            msg = 'P{seen}'.format(
                seen=self.total_seen
            )
        return '[W{worker_no}: {msg}]'.format(
            worker_no=self.worker_no,
            msg=msg
        )


class BusyLock(Lock):
    def acquire_now(self):
        if not self._locked and all(w.cancelled() for w in self._waiters):
            self._locked = True
            return True
        else:
            return False


class MalformedResponse(Exception):
    """Raised when server response is malformed"""


class CaptchaException(Exception):
    """Raised when a CAPTCHA is needed."""
