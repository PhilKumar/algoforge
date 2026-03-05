#!/usr/bin/env python3
import re
import unicodedata

with open('strategy.html', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Real emoji (skin-tone emoji, objects, symbols, etc.) - exclude box drawing & decorative
emoji_pattern = re.compile(
    '['
    '\U0001F300-\U0001FAFF'   # Misc Symbols, Emoticons, Transport, etc.
    '\U00002702-\U000027B0'   # Dingbats
    '\U00002600-\U000026FF'   # Misc symbols (sun, stars, etc.)
    '\U00002B50-\U00002B55'   # Stars
    '\U0000FE0F'              # Variation selector
    '\U0000200D'              # ZWJ
    '\U00002934-\U00002935'   # Arrows supplement
    '\U000023E9-\U000023FA'   # Media symbols  
    '\U00002328'              # Keyboard
    '\U000023CF'              # Eject
    ']'
)

# Arrows and math symbols of interest (not decorative box lines)
symbol_pattern = re.compile(r'[▼▲►◄●○★☆♦♠♣♥↑↓←→✓✕✖×÷±∞≈≠≤≥▶◀⬆⬇➡⬅⏱⚡]')

entity_pattern = re.compile(r'&#x[0-9A-Fa-f]+;')
svg_pattern = re.compile(r'<svg[^>]*>', re.IGNORECASE)
icon_class_pattern = re.compile(r'class\s*=\s*["\'][^"\']*(?:fa-|icon-|bi-|material-icon|glyphicon)[^"\']*["\']', re.IGNORECASE)

results = []

for i, line in enumerate(lines, 1):
    found_on_line = set()
    
    for m in emoji_pattern.finditer(line):
        char = m.group()
        # Skip variation selectors and ZWJ when standalone
        if char in ('\uFE0F', '\u200D'):
            continue
        key = (i, char)
        if key not in found_on_line:
            found_on_line.add(key)
            name = unicodedata.name(char, '?')
            ctx = line.strip()[:200]
            results.append((i, char, f'U+{ord(char):04X}', name, ctx))

    for m in symbol_pattern.finditer(line):
        char = m.group()
        key = (i, char)
        if key not in found_on_line:
            found_on_line.add(key)
            name = unicodedata.name(char, '?')
            ctx = line.strip()[:200]
            results.append((i, char, f'U+{ord(char):04X}', name, ctx))

    for m in entity_pattern.finditer(line):
        entity = m.group()
        ctx = line.strip()[:200]
        # Try to decode the entity
        try:
            cp = int(entity[3:-1], 16)
            decoded = chr(cp)
            name = unicodedata.name(decoded, '?')
            results.append((i, f'{decoded} {entity}', f'U+{cp:04X}', name, ctx))
        except:
            results.append((i, entity, 'HTML-entity', '?', ctx))

    for m in svg_pattern.finditer(line):
        ref = m.group()[:100]
        ctx = line.strip()[:200]
        results.append((i, ref, 'SVG-element', 'SVG icon', ctx))

    for m in icon_class_pattern.finditer(line):
        ref = m.group()[:100]
        ctx = line.strip()[:200]
        results.append((i, ref, 'icon-class', 'Icon font', ctx))

# Print deduplicated results
print(f"=== COMPLETE ICON/EMOJI INVENTORY: strategy.html ===")
print(f"Total unique icon occurrences: {len(results)}\n")

# Group by type for summary
from collections import Counter
emoji_summary = Counter()
for _, icon, code, name, _ in results:
    if 'SVG' not in code and 'icon' not in code and 'HTML' not in code:
        emoji_summary[icon] += 1

print("--- EMOJI SUMMARY (unique characters & counts) ---")
for char, count in sorted(emoji_summary.items(), key=lambda x: -x[1]):
    name = unicodedata.name(char, '?') if len(char) == 1 else '?'
    print(f"  {char}  (count={count}) - {name}")
print()

print("--- DETAILED LINE-BY-LINE LIST ---")
for line_num, icon, code, name, ctx in results:
    print(f"Line {line_num}: {icon}  [{code}] {name}")
    print(f"  > {ctx[:180]}")
    print()
