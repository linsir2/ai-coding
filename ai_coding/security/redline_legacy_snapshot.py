"""The frozen snapshot of the pre-merge ``bash_denied`` (M-D2, U9).

M-D2 merged the two red-line readers: ``permission_gate.bash_denied`` used to
own the whole judgement (the shell grammar, then the text fallback for a line
the grammar could not read), and it now delegates to
``bash_redlines.bash_red_line_with_fallback``.  The merge had to be
behaviour-preserving, and the safety net for that was a comparison between the
two.

That comparison could not be written against the *live* ``bash_denied`` any
more: both sides called the same function, so the assertion was true by
construction and would stay true no matter what the merge did to the answer.
The snapshot below is the missing half -- the pre-merge ``bash_denied``'s
verdict for every command in the corpus and in the existing red-line test set,
captured once and stored here, independent of the code under test.  U9 compares
the live reader against *this*, so a divergence in either direction is a red
build rather than a tautology.

The values were captured from the implementation the day the merge landed
(``bash_denied`` had not yet been made a delegate), which is why they agree
with ``security/redline_corpus.py``'s frozen verdicts -- that corpus was
captured the same way, and the golden comparison in the benchmark is the other
half of the same proof.

This module reads no clock and no file; the frozen values are the point.

**Re-frozen after §C.** The snapshot predates the known_miss fix, so the 14
commands whose verdict §C changed (a word carrying an expansion is no longer
dropped, wrappers are peeled past their values, ``doas`` and
``--force-with-lease`` are recognised, and an unresolvable program name is
refused) were updated to the new answers.  The other rows are untouched: the
snapshot still proves the merge is behaviour-preserving everywhere §C did not
intend a change.
"""

from __future__ import annotations

__all__ = ["LEGACY_BASH_DENIED"]

#: The pre-merge ``bash_denied``'s verdict per command: the corpus rows plus the
#: DENY/WARN halves of ``tests/test_bash_red_lines.py``.  An empty string is the
#: "ask, do not refuse" verdict.
LEGACY_BASH_DENIED: dict[str, str] = {
    '# rm -rf /': '',
    '$(rm -rf /)': 'recursive root deletion (rm -rf /)',
    '> out.txt': '',
    'D=/data; rm -rf $D': '',
    'FOO=bar rm -rf /': 'recursive root deletion (rm -rf /)',
    'RM -RF /': 'recursive root deletion (RM -RF /)',
    'a && rm -rf /': 'recursive root deletion (rm -rf /)',
    'cat $(rm -rf /)': 'recursive root deletion (rm -rf /)',
    'cat .env': 'credential file (.env)',
    'cat .env &&': 'credential file (.env)',
    'cat .env.example': '',
    'cat /etc/shadow': 'credential file (etc/shadow)',
    'cat >': '',
    'cat README.md': '',
    'cat notes.md  # do not rm -rf / here': '',
    'cat src/config.py': '',
    'cat ~/.aws/credentials': 'credential file (.aws)',
    'cat ~/.aws/credentials &&': 'credential file (.aws)',
    'cat ~/.gnupg/secring.gpg': 'credential file (.gnupg)',
    'cat ~/.ssh/config': 'credential file (.ssh)',
    # §C: the word carrying the expansion is no longer dropped, so the
    # credential directory component is seen.
    'cat ~/.ssh/id_$USER': 'credential file (.ssh)',
    'cat ~/.ssh/id_rsa': 'credential file (id_rsa)',
    'chmod  777  /tmp/x': 'world-writable permissions (chmod 777 /tmp/x)',
    'chmod +rwx /tmp/x': 'world-writable permissions (chmod +rwx /tmp/x)',
    'chmod 0777 ./build': 'world-writable permissions (chmod 0777 ./build)',
    'chmod 0777 /tmp/x': 'world-writable permissions (chmod 0777 /tmp/x)',
    'chmod 644 script.sh': '',
    'chmod 777': 'world-writable permissions (chmod 777)',
    'chmod 777 .': 'world-writable permissions (chmod 777 .)',
    'chmod 777 ./build': 'world-writable permissions (chmod 777 ./build)',
    'chmod 777 /tmp/x': 'world-writable permissions (chmod 777 /tmp/x)',
    'chmod 777 /tmp/x &&': 'world-writable permissions (chmod 777)',
    'chmod 777 build': 'world-writable permissions (chmod 777 build)',
    'chmod a+rwx ./dist': 'world-writable permissions (chmod a+rwx ./dist)',
    'chmod a+rwx /tmp/x': 'world-writable permissions (chmod a+rwx /tmp/x)',
    'chmod a+w /tmp/x': 'world-writable permissions (chmod a+w /tmp/x)',
    'command sudo ls': 'sudo escalation (command sudo ls)',
    'dd if=/dev/sda of=/tmp/x': 'disk write (dd if=/dev/sda of=/tmp/x)',
    'dd if=/dev/sda of=/tmp/x "unclosed': "disk write ('dd if=')",
    'dd if=/dev/zero of=/dev/sda': 'disk write (dd if=/dev/zero of=/dev/sda)',
    'dd if=/dev/zero of=/tmp/out.bin': '',
    'dd of=/dev/sda if=/dev/zero': 'disk write (dd of=/dev/sda if=/dev/zero)',
    'doas rm x': 'sudo escalation (doas rm x)',
    'echo "chmod 777 /tmp/x': "world-writable permissions ('chmod 777')",
    'echo "rm -rf /': "recursive root deletion ('rm -rf /')",
    'echo "rm -rf /"': '',
    'echo "sudo ': '',
    'echo "sudo "': '',
    'echo "unclosed': '',
    "echo 'rm -rf /'": '',
    'echo a &&': '',
    'echo done': '',
    'echo shutdown': '',
    'echo x > /dev/sda': 'raw disk write (echo x)',
    'echo x > /dev/sda &&': "raw disk write ('> /dev/sd')",
    'echo x >/dev/sda': 'raw disk write (echo x)',
    'env -i sudo ls': 'sudo escalation (env -i sudo ls)',
    'env sudo ls': 'sudo escalation (env sudo ls)',
    'exec sudo ls': 'sudo escalation (exec sudo ls)',
    "find . -name '*.py'": '',
    'git -C /tmp push --force': 'force git push (git -C /tmp push --force)',
    'git push --force': 'force git push (git push --force)',
    'git push --force origin main': 'force git push (git push --force origin main)',
    'git push --force origin main &&': "force git push ('git push --force')",
    'git push --force-with-lease origin main': (
        'force git push (git push --force-with-lease origin main)'
    ),
    'git push -f': 'force git push (git push -f)',
    'git push -f origin': 'force git push (git push -f origin)',
    'git push origin main': '',
    'git status': '',
    'grep -r "chmod 777" docs/': '',
    'grep -r TODO src/': '',
    'grep -r password src/ tests/': '',
    'ls -la': '',
    'ls |': '',
    'ls | rm -rf /': 'recursive root deletion (rm -rf /)',
    'ls; chmod 777 /tmp/x': 'world-writable permissions (chmod 777 /tmp/x)',
    'ls; rm -rf /': 'recursive root deletion (rm -rf /)',
    'mkfs --help': 'filesystem format (mkfs --help)',
    'mkfs /dev/sda1': 'filesystem format (mkfs /dev/sda1)',
    'mkfs /dev/sda1 "unterminated': "filesystem format ('mkfs')",
    'mkfs.ext4 ./disk.img': 'filesystem format (mkfs.ext4 ./disk.img)',
    'mkfs.ext4 /dev/sda1': 'filesystem format (mkfs.ext4 /dev/sda1)',
    'mkfsdoc --help': '',
    'nohup sudo ls': 'sudo escalation (nohup sudo ls)',
    "printf '%s' \"sudo ": '',
    'printf done &&': '',
    'pwd': '',
    'python -m pytest -q': '',
    'reboot': 'system reboot (reboot)',
    'reboot &&': "system reboot ('reboot')",
    'reboot --help': 'system reboot (reboot --help)',
    'rm  -rf  /': 'recursive root deletion (rm -rf /)',
    'rm --recursive --force /': 'recursive root deletion (rm --recursive --force /)',
    'rm -fr /': 'recursive root deletion (rm -fr /)',
    'rm -r -f /': 'recursive root deletion (rm -r -f /)',
    'rm -rf "$(echo /)"': 'recursive root deletion (rm -rf)',
    'rm -rf $HOME': 'recursive root deletion (rm -rf $HOME)',
    'rm -rf $PWD': '',
    'rm -rf ${HOME}': 'recursive root deletion (rm -rf ${HOME})',
    'rm -rf ./..': 'recursive root deletion (rm -rf ./..)',
    'rm -rf ./build': '',
    'rm -rf /': 'recursive root deletion (rm -rf /)',
    'rm -rf / "unterminated': 'recursive root deletion (rm -rf /)',
    'rm -rf / &&': 'recursive root deletion (rm -rf /)',
    'rm -rf / && echo done': 'recursive root deletion (rm -rf /)',
    'rm -rf /*': 'recursive root deletion (rm -rf /*)',
    'rm -rf /bin': 'recursive root deletion (rm -rf /bin)',
    'rm -rf /boot': 'recursive root deletion (rm -rf /boot)',
    'rm -rf /etc': 'recursive root deletion (rm -rf /etc)',
    'rm -rf /home/user': 'recursive root deletion (rm -rf /home/user)',
    'rm -rf /lib': 'recursive root deletion (rm -rf /lib)',
    'rm -rf /opt': 'recursive root deletion (rm -rf /opt)',
    'rm -rf /root': 'recursive root deletion (rm -rf /root)',
    'rm -rf /tmp/build': '',
    'rm -rf /usr': 'recursive root deletion (rm -rf /usr)',
    'rm -rf /var/log': 'recursive root deletion (rm -rf /var/log)',
    'rm -rf ~': 'recursive root deletion (rm -rf ~)',
    'rm -rf ~/': 'recursive root deletion (rm -rf ~/)',
    'sed -n 1,5p README.md': '',
    'sh -c "rm -rf /': "recursive root deletion ('rm -rf /')",
    'shutdown now': 'system shutdown (shutdown now)',
    'sudo': 'sudo escalation (sudo)',
    'sudo  ls': 'sudo escalation (sudo ls)',
    'sudo -n true': 'sudo escalation (sudo -n true)',
    'sudo -u deploy ls': 'sudo escalation (sudo -u deploy ls)',
    'sudo echo hi': 'sudo escalation (sudo echo hi)',
    'sudo ls &&': "sudo escalation ('sudo ')",
    'sudo rm x': 'sudo escalation (sudo rm x)',
    'sudo su -': 'sudo escalation (sudo su -)',
    'time sudo ls': 'sudo escalation (time sudo ls)',
    'timeout 5 sudo ls': 'sudo escalation (timeout 5 sudo ls)',
    'x="rm -rf /"; $x': 'unauditable command ($x)',
    'x=$(rm -rf /)': 'recursive root deletion (rm -rf /)',
    'x=1; $x rm -rf /': 'unauditable command ($x rm -rf /)',
    'x=1; rm -rf /': 'recursive root deletion (rm -rf /)',
}
