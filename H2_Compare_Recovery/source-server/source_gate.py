"""Start a fresh pinned local R4 source and prove replay and search before comparison."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256, file_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from threading import Thread
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler, Request

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parents[1] / 'H1_Validate_the_Receiver/local-wsl-v3/fixtures'
VERSION = '2026.708.1348'
COMMIT = 'f04a1d4bdcf0265eb401b98eb0647a4ff373813e'
PACKAGE_SHA256 = '55ac4cc583c229664f5a528d5771cf75d24aaf5e7a70ec7a20413084b8091317'
SUB = 'encounter-start'
PATIENT = 'drill-patient-1'
TOPIC = 'http://hl7.org/fhir/us/ecr/SubscriptionTopic/encounter-start'
REPLAY = f'Subscription/{SUB}/$events?eventsSinceNumber=3&eventsUntilNumber=3&content=full-resource'
SEARCH = f'Encounter?patient={PATIENT}'
http = build_opener(ProxyHandler({}))


def save(path, obj):
    path.write_text(json.dumps(obj, indent=2) + '\n', encoding='utf-8')


def require(ok, message):
    if not ok:
        raise ValueError(message)


def fixture(n):
    bundle = json.loads((FIXTURES / f'admission-{n}.json').read_bytes())
    return bundle['entry'][1]['resource']


def call(folder, label, base, path, data=None, expected=(200,)):
    method = 'PUT' if data is not None else 'GET'
    if method == 'PUT':
        path += '/' + data['id']
    url = base + '/' + path
    require(urlsplit(url).hostname == '127.0.0.1', 'Only the private source is allowed')
    body = None if data is None else json.dumps(data, ensure_ascii=False).encode()
    if body is not None:
        (folder / f'{label}.request').write_bytes(body)
    headers = {'Accept': 'application/fhir+json', 'Content-Type': 'application/fhir+json'}
    req = Request(url, data=body, headers=headers, method=method)
    metadata = {'method': method, 'url': url, 'headers':headers, 'start': datetime.now(timezone.utc).isoformat()}
    save(folder / f'{label}.json', metadata)
    try:
        response = http.open(req, timeout=20)
    except HTTPError as exc:
        response = exc
    with response:
        status, raw = response.status, response.read()
        (folder / f'{label}.response').write_bytes(raw)
        metadata.update(status=status, response_headers=dict(response.headers), end=datetime.now(timezone.utc).isoformat())
        save(folder / f'{label}.json', metadata)
    require(status in expected, f'{label} returned {status}, see {label}.response')
    return json.loads(raw) if raw else {}


def check_clinical(record, original):
    # FHIR server makes its own resource version and timestamp on persistence.
    value = deepcopy(record)
    meta = value.get('meta', {})
    meta.pop('versionId', None)
    meta.pop('lastUpdated', None)
    if 'meta' in value and not meta:
        del value['meta']
    require(value == original, 'Returned Encounter differs from the submitted clinical fixture')


def verify(folder, base):
    capability = call(folder, 'metadata', base, 'metadata')
    require(capability.get('fhirVersion') == '4.0.1', 'Expected R4 release 4.0.1')
    software = capability.get('software', {})
    require(software.get('name') == 'fhir-candle' and software.get('version') == '0.2026.708.1348', 'Unexpected live server software identity')
    replay = call(folder, 'replay-event-3', base, REPLAY)
    require(replay.get('resourceType') == 'Bundle' and replay.get('type') == 'history', 'Expected R4 history Bundle')
    entries = replay['entry']
    status = entries[0]['resource']
    require(status['resourceType'] == 'Parameters', 'Missing R4 status')
    params = status['parameter']
    ref = next(p['valueReference']['reference'] for p in params if p['name'] == 'subscription')
    require(ref in (f'Subscription/{SUB}', base + f'/Subscription/{SUB}'), 'Unexpected subscription')
    kind = next(p['valueCode'] for p in params if p['name'] == 'type')
    require(kind == 'query-event', 'Missing query-event type')
    events = [p['part'] for p in params if p['name'] == 'notification-event']
    require(len(events) == 1, 'Expected exactly one replayed event')
    number = next(v for p in events[0] if p['name'] == 'event-number' for k,v in p.items() if k.startswith('value'))
    require(str(number) == '3', 'Unexpected replayed event number')
    focus = next(p['valueReference']['reference'] for p in events[0] if p['name'] == 'focus')
    relative = 'Encounter/drill-adm-3'
    require(focus in (relative, base + '/' + relative), 'Wrong event focus')
    returned = [e['resource'] for e in entries[1:] if e.get('resource', {}).get('resourceType') == 'Encounter' and e['resource'].get('id') == 'drill-adm-3']
    if not returned:
        returned = [call(folder, 'replay-focus-3', base, relative)]
    require(len(returned) == 1, 'Replayed focus ambiguous')
    check_clinical(returned[0], fixture(3))
    search = call(folder, 'search-patient-encounters', base, SEARCH)
    require(search.get('resourceType') == 'Bundle' and search.get('type') == 'searchset', 'Expected R4 searchset')
    matches = [e['resource'] for e in search.get('entry', []) if e.get('search',{}).get('mode','match') == 'match']
    require(len(matches) == 4 and {e.get('id') for e in matches} == {f'drill-adm-{n}' for n in range(1,5)}, 'Expected four distinct patient Encounters on search')
    require(all(e.get('subject',{}).get('reference') == 'Patient/' + PATIENT for e in matches), 'Wrong patient in search result')
    check_clinical(next(e for e in matches if e['id']=='drill-adm-3'), fixture(3))
    return {'replay_response':'replay-event-3.response','search_response':'search-patient-encounters.response',
            'focus':focus,'event_number':3, 'search_matches':len(matches),
            'encounter_sha256':sha256(json.dumps(returned[0],sort_keys=True).encode()).hexdigest()}


def create_run():
    directory = HERE / 'runs'
    directory.mkdir(exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-'),dir=directory))
    save(folder / 'expected.json', {'scope':'H2 source prerequisites', 'before_comparison':True,
        'replay':'Only event 3 of the created subscription is returned; focus yields the complete seeded third admission.',
        'search':'GET Encounter?patient=drill-patient-1 returns four seeded admissions, including the complete third admission.',
        'comparison':'No comparison runs during the prerequisite test.'})
    return folder


def seed(folder, base, bin_dir, callback_url):
    topic_path = bin_dir / 'fhirData/hl7.fhir.us.ecr/r4/basic-subscriptiontopic-encounter-started.json'
    call(folder, 'seed-topic', base, 'Basic', json.loads(topic_path.read_bytes()), expected=(201,))
    call(folder, 'seed-patient', base, 'Patient', {'resourceType':'Patient','id':PATIENT}, expected=(201,))
    subscription = {'resourceType':'Subscription','id':SUB,'status':'requested','reason':'H2 local admission test',
        'criteria':TOPIC,'channel':{'type':'rest-hook','endpoint':callback_url,'payload':'application/fhir+json',
            '_payload':{'extension':[{'url':'http://hl7.org/fhir/uv/subscriptions-backport/StructureDefinition/backport-payload-content','valueCode':'full-resource'}]}}}
    call(folder, 'seed-subscription', base, 'Subscription', subscription, expected=(201,))
    for i in range(30):
        state = call(folder, f'check-subscription-{i:02}', base, 'Subscription/' + SUB)
        if state.get('status')=='active':
            break
        time.sleep(1)
    else:
        raise ValueError('Subscription did not become active')
    for n in range(1,5):
        call(folder, f'seed-admission-{n}',base,'Encounter',fixture(n),expected=(201,))


def verify_existing(prior_gate_path, output_dir):
    """Perform two new reads of the existing source and write a new gate receipt."""
    folder = Path(output_dir)
    folder.mkdir(parents=True, exist_ok=True)
    require(not any(folder.iterdir()), 'Use a new or empty gate evidence directory')
    original = json.loads(Path(prior_gate_path).read_bytes())
    require(original['result'] == 'PASS' and original['server_version'] == VERSION
            and original['server_commit'] == COMMIT and original['nuget_sha256'] == PACKAGE_SHA256,
            'Expected passing pinned source setup receipt')
    base = original['base_url']
    save(folder/'expected.json', {'replay':REPLAY,'search':SEARCH,'expect':'One full matching third Encounter from event 3; four patient Encounters including identical clinical admission 3.'})
    receipt = {k:original[k] for k in ('server_version','server_commit','nuget_sha256','base_url','subscription_id','patient_id','replay_path','search_path')}
    receipt['original_gate'] = str(Path(prior_gate_path).resolve())
    try:
        receipt.update(verify(folder, base),result='PASS')
    except Exception as exc:
        receipt.update(result='BLOCKED',reason=str(exc))
        raise
    finally:
        receipt['ended_utc'] = datetime.now(timezone.utc).isoformat()
        save(folder/'gate.json',receipt)
    return receipt


def run(args):
    folder = create_run()
    print('Evidence:', folder, flush=True)
    receipt = {'server_version':VERSION,'server_commit':COMMIT,'nuget_sha256':PACKAGE_SHA256,
        'patient_id':PATIENT,'subscription_id':SUB, 'replay_path':REPLAY,'search_path':SEARCH}
    process = sink = log = None
    try:
        if args.verify:
            original = json.loads(args.verify.read_bytes())
            require(original['result']=='PASS' and original['server_version']==VERSION,'Expected passing setup receipt')
            base = original['base_url']
            receipt['original_gate'] = str(args.verify.resolve())
        else:
            runtime = args.runtime.resolve()
            bin_dir = runtime / 'candle/tools/net8.0/any'
            with (runtime/'candle.nupkg').open('rb') as stream:
                require(file_digest(stream,'sha256').hexdigest()==PACKAGE_SHA256,'Unexpected NuGet archive')
            env = dict(os.environ, LD_LIBRARY_PATH=str(runtime/'icu/usr/lib/x86_64-linux-gnu'),
                       XDG_DATA_HOME=str(runtime/'local-data'))
            command = [str(runtime/'dotnet/dotnet'),str(bin_dir/'fhir-candle.dll')]
            version = subprocess.check_output(command+['--version'],env=env,text=True).strip()
            require(version=='2026.0708.1348+'+COMMIT,'Unexpected candle executable version')
            with socket.socket() as s:
                s.bind(('127.0.0.1',0))
                port = s.getsockname()[1]
            origin = f'http://127.0.0.1:{port}'
            env['Kestrel__Endpoints__H2Private__Url'] = origin
            base = origin + '/fhir/r4'
            class Handler(BaseHTTPRequestHandler):
                def do_POST(self):
                    if self.path != '/hook':
                        self.send_error(404)
                        return
                    data = self.rfile.read(int(self.headers['Content-Length']))
                    name = 'callback-' + str(time.time_ns())
                    (folder/(name+'.body')).write_bytes(data)
                    save(folder/(name+'.json'), {'headers':dict(self.headers),'path':self.path,'time':datetime.now(timezone.utc).isoformat()})
                    self.send_response(200)
                    self.end_headers()
                def log_message(self, *values):
                    pass
            sink = ThreadingHTTPServer(('127.0.0.1',0),Handler)
            Thread(target=sink.serve_forever,daemon=True).start()
            log = (folder/'server.log').open('w')
            command += ['--url',origin,'--port',str(port),'--r4','r4','--headless','--create-as-update','--max-subscription-minutes','1440','--fhir-package-cache',str(runtime/'cache')]
            save(folder/'server-command.json', {'args':command,'executable_version':version,'listen_url':origin})
            process = subprocess.Popen(command,cwd=bin_dir,env=env,stdout=log,stderr=subprocess.STDOUT)
            receipt.update(base_url=base,server_pid=process.pid)
            for _ in range(60):
                if process.poll() is not None:
                    raise RuntimeError('Source exited during startup; see server.log')
                try:
                    with http.open(base+'/metadata',timeout=2) as response:
                        if response.status==200:
                            break
                except (URLError,TimeoutError):
                    pass
                time.sleep(.5)
            else:
                raise ValueError('R4 server failed readiness')
            listeners=subprocess.check_output(['ss','-Hltn'],text=True)
            addresses=[line.split()[3] for line in listeners.splitlines() if line.split()[3].endswith(':'+str(port))]
            require(addresses==['127.0.0.1:'+str(port)], 'Expected a loopback-only source listener')
            receipt['listen_addresses']=addresses
            seed(folder,base,bin_dir,f'http://127.0.0.1:{sink.server_port}/hook')
        receipt['base_url']=base
        receipt.update(verify(folder,base),result='PASS',ended_utc=datetime.now(timezone.utc).isoformat())
        save(folder/'gate.json',receipt)
        print('PASS:', folder/'gate.json',flush=True)
        if process is not None:
            print('Private source stays live until this process receives an interrupt.', flush=True)
            try:
                process.wait()
            except KeyboardInterrupt:
                print('Stopping the private source after its passing gate.', flush=True)
        return 0
    except (OSError, RuntimeError, ValueError, KeyError, StopIteration, subprocess.SubprocessError) as exc:
        receipt.update(result='BLOCKED', reason=f'{type(exc).__name__}: {exc}',ended_utc=datetime.now(timezone.utc).isoformat())
        save(folder/'gate.json',receipt)
        print('BLOCKED:', receipt['reason'],flush=True)
        return 2
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=15)
        if sink:
            sink.shutdown()
            sink.server_close()
        if log:
            log.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',type=Path,default=Path('/tmp/h2-candle-runtime'))
    parser.add_argument('--verify',type=Path,help='Repeat both live reads against an existing passing source')
    sys.exit(run(parser.parse_args()))
