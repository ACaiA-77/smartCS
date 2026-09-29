"""Pin completed candidate outputs, excluding command logs still being appended."""
import json
from build_candidates import OUT, digest

path = OUT / 'generation_manifest.json'
data = json.loads(path.read_text(encoding='utf-8'))
data['output_hashes'] = {name: digest(OUT / name) for name in data['output_hashes']
                         if not name.endswith('.log') and not name.endswith('_command.json')}
data['hash_scope_note'] = 'Candidate data/evidence hashes only; command logs have their separate exit records. Not gold freezing.'
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print('Completed candidate artifact hashes recorded; mutable command logs excluded.')
