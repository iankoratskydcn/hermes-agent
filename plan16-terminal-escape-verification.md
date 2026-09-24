<!-- no-tmp: ok — raw captured verification transcript; paths are literal evidence of the actual run, not a scratch-dir instruction -->
REQUIRE_BASE /tmp/hermes-plan16-kih3_zfz
<!-- no-tmp: ok — raw captured verification transcript; paths are literal evidence of the actual run, not a scratch-dir instruction -->
REQUIRE_PROJECTION /tmp/hermes-plan16-kih3_zfz/archive/repo
REQUIRE_COMMAND cd ../..; printf 'READ='; cat outside-marker.txt; printf 'ESCAPE-WRITE\n' > outside-marker.txt
<!-- no-tmp: ok — raw captured verification transcript; paths are literal evidence of the actual run, not a scratch-dir instruction -->
REQUIRE_RESULT {'output': 'READ=cat: outside-marker.txt: No such file or directory\n/usr/bin/bash: line 4: outside-marker.txt: Permission denied\n', 'returncode': 1, 'cwd_observed': True, 'cwd': '/tmp/hermes-plan16-kih3_zfz'}
REQUIRE_OUTSIDE 'SECRET-UNCHANGED\n'
AVAILABLE_COMMAND printf AVAILABLE_OK
<!-- no-tmp: ok — raw captured verification transcript; paths are literal evidence of the actual run, not a scratch-dir instruction -->
AVAILABLE_RESULT {'output': 'AVAILABLE_OK', 'returncode': 0, 'cwd_observed': True, 'cwd': '/tmp/hermes-plan16-kih3_zfz/archive/repo'}
MISSING_CAPABILITY_EXCEPTION SandboxUnavailable sandbox required but bubblewrap (bwrap) is unavailable
