"""Real-entrypoint s6 acceptance check for the disposable integration suite."""

import json
import time


def check_s6_guard(suite):
    if not suite.dev.startswith("ai-dev-audit-"):
        raise RuntimeError("s6 acceptance requires a disposable audit container")
    suite.py(
        "from pathlib import Path\n"
        "root=Path('/home/dev/.ai-data/s6-services')\n"
        "healthy=root/'audit-healthy';healthy.mkdir()\n"
        "(healthy/'type').write_text('longrun\\n')\n"
        "(healthy/'run').write_text(\"#!/bin/sh\\nexec /command/s6-setuidgid dev /bin/sh -c "
        "'printf ready > /home/dev/workspace/s6-healthy-started; exec sleep 3600'\\n\")\n"
        "(healthy/'run').chmod(0o755)\n"
        "bundle=root/'audit-group';bundle.mkdir()\n"
        "(bundle/'type').write_text('bundle\\n')\n"
        "(bundle/'contents').write_text('audit-healthy\\n')\n"
        "cycle=root/'audit-cycle';cycle.mkdir()\n"
        "(cycle/'type').write_text('bundle\\n')\n"
        "(cycle/'contents.d').mkdir();(cycle/'contents.d/user').touch()\n"
        "(root/'user/contents.d').rmdir()\n"
        "(root/'user/contents').write_text('audit-group\\naudit-cycle\\n')\n"
        "bad=root/'audit-broken';bad.mkdir()\n"
        "(bad/'type').write_text('longrun\\n')\n"
        "(bad/'run').write_text('#!/bin/sh\\nexec sleep 3600\\n');(bad/'run').chmod(0o755)\n"
        "(bad/'dependencies.d').mkdir();(bad/'dependencies.d/audit-absent').touch()\n",
        root=True,
    )
    for _ in range(2):
        suite.docker("restart", suite.dev)
        suite.wait_ssh()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = suite.docker(
                "exec", "-u", "0", suite.dev, "/command/s6-svstat", "/run/service/audit-healthy",
                check=False,
            )
            if status.returncode == 0 and status.stdout.startswith("up "):
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("Healthy custom service did not stay supervised: " + status.stderr.strip())
        state = json.loads(suite.py(
            "import json\nfrom pathlib import Path\n"
            "root=Path('/home/dev/.ai-data/s6-services')\n"
            "copies=list((root/'.disabled-by-guard').glob('audit-broken.*'))\n"
            "print(json.dumps({'healthy_enabled':(root/'user/contents.d/audit-group').exists(),"
            "'healthy_ran':Path('/home/dev/workspace/s6-healthy-started').read_text()=='ready',"
            "'bad_removed':not(root/'audit-broken').exists(),"
            "'archived':len(copies)==1 and (copies[0]/'run').exists() "
            "and (copies[0]/'disabled-reason.txt').exists(),"
            "'cycle_not_enabled':not(root/'user/contents.d/audit-cycle').exists(),"
            "'legacy_group_kept':(root/'audit-group/contents').exists() "
            "and not(root/'audit-group/contents.d').exists(),"
            "'healthy_not_quarantined':not list((root/'.disabled-by-guard').glob('audit-healthy.*'))}))"
        ))
        assert all(state.values()), state
    return "Bad definition and cyclic activation isolated; legacy nested service survives two real restarts"
