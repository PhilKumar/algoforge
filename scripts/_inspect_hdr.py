with open("strategy.html", "r", encoding="utf-8") as f:
    content = f.read()
idx = content.find("completedTradesTable")
if idx != -1:
    t = content.find("<thead>", idx)
    print("THEAD:", repr(content[t : t + 700]))
else:
    print("completedTradesTable not found")
