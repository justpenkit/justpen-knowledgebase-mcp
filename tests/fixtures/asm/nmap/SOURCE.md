# nmap

- **Command:** `nmap -sV -p 22,443 --open --script ssh-hostkey --script-args ssh_hostkey=sha256 -oX scan.xml www.example.com`
- **Version:** Nmap 7.95, XML output version 1.05
- **Schema:** [nmap.dtd](https://github.com/nmap/nmap/blob/master/docs/nmap.dtd);
    CPE URIs per [the Nmap book](https://nmap.org/book/output-formats-cpe.html);
    the ssh-hostkey table and `output` per [ssh-hostkey.nse](https://github.com/nmap/nmap/blob/master/scripts/ssh-hostkey.nse)
    and [`ssh1.fingerprint_base64`](https://github.com/nmap/nmap/blob/master/nselib/ssh1.lua).

Derived from the documented schema, not recorded from a live target. Addresses are documentation ranges.

- `--open` keeps only open ports, so every `port` element is written; `extraports` is absent with two probed ports.
- The SSH server presents one host key. Every `elem` of an ssh-hostkey table shares one path, so the
    algorithm row keeps only the text that names a `host_key` algorithm, and the fingerprint comes from the
    single `SHA256:` token of `output` through `openssh_b64_to_hex`.
- Each service carries one application CPE. `technology.name` comes from `product`, so a second (OS) CPE on
    one service would have no name source.
