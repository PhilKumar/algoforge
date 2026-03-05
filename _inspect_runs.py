import json
runs = json.load(open('runs.json'))
paper = [r for r in runs if r.get('mode') == 'paper']
print(f"Total runs: {len(runs)}, paper: {len(paper)}")
if paper:
    r = paper[-1]
    print('paper run id:', r.get('id'))
    print('keys:', list(r.keys()))
    print('created_at:', r.get('created_at'))
    print('start_time:', r.get('start_time'))
    print('end_time:', r.get('end_time'))
    trades = r.get('trades') or r.get('closed_trades') or []
    print(f'trades count: {len(trades)}')
    if trades:
        t = trades[0]
        print('trade keys:', list(t.keys()))
        print('entry_time:', t.get('entry_time'))
        print('exit_time:', t.get('exit_time'))
