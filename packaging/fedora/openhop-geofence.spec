# RPM spec for Fedora and compatible distributions.
# Build from the sdist (python3 -m build --sdist), e.g.:
#   cp packaging/fedora/openhop-geofence.sysusers dist/
#   rpmbuild -ba --define "_sourcedir $PWD/dist" packaging/fedora/openhop-geofence.spec
# Keep Version and %%changelog in step with src/openhop_geofence/__init__.py
# (checked by packaging/check-version.sh).

Name:           openhop-geofence
Version:        0.1.0
Release:        1%{?dist}
Summary:        Geofenced Repeat/Monitor control for an OpenHop MeshCore repeater

License:        MIT
URL:            https://github.com/ewsandor/openhop-geofence-service
Source0:        openhop_geofence-%{version}.tar.gz
# Also inside Source0; separate because %%sysusers_create_compat reads it before %%prep.
Source1:        openhop-geofence.sysusers

BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  systemd-rpm-macros

Requires:       python3 >= 3.11
Requires:       polkit
%{?sysusers_requires_compat}
# Optional features; Leaflet (map) is not packaged in Fedora, see README.md.
Recommends:     python3-shapely
Recommends:     python3-flask
Recommends:     python3-waitress
Recommends:     python3-tomlkit
Suggests:       gpsd
Suggests:       gpsd-clients

%description
Switches an OpenHop MeshCore repeater between Repeat (forward) and Monitor
(or no_tx) based on configured GPS zones and optional MeshMapper coverage.
Includes hysteresis, a safe mode latch, an optional web interface with a map,
and an optional MeshCore companion for reports and commands.

The service is installed disabled. Review /etc/openhop-geofence/config.toml
and enable it with "systemctl enable --now openhop-geofence".

%prep
%autosetup -n openhop_geofence-%{version}

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files openhop_geofence
install -Dpm 0644 packaging/openhop-geofence.service %{buildroot}%{_unitdir}/openhop-geofence.service
install -Dpm 0644 packaging/50-openhop-geofence-reboot.rules %{buildroot}%{_datadir}/polkit-1/rules.d/50-openhop-geofence-reboot.rules
install -Dpm 0644 %{SOURCE1} %{buildroot}%{_sysusersdir}/openhop-geofence.conf
install -dm 2775 %{buildroot}%{_sysconfdir}/openhop-geofence
install -dm 2755 %{buildroot}%{_sysconfdir}/openhop-geofence/secrets
install -pm 0664 config/config.toml.example %{buildroot}%{_sysconfdir}/openhop-geofence/config.toml
install -Dpm 0644 config/config.toml.example %{buildroot}%{_datadir}/openhop-geofence/config.toml.example
install -dm 0750 %{buildroot}%{_sharedstatedir}/openhop-geofence
install -dm 0750 %{buildroot}%{_localstatedir}/cache/openhop-geofence

%check
PYTHONPATH=%{buildroot}%{python3_sitelib}:tests PYTHONDONTWRITEBYTECODE=1 %{python3} -m unittest discover -s tests

%pre
%sysusers_create_compat %{SOURCE1}

%post
%systemd_post openhop-geofence.service

%preun
%systemd_preun openhop-geofence.service

%postun
%systemd_postun_with_restart openhop-geofence.service

%files -f %{pyproject_files}
%license LICENSE
%doc README.md DESIGN.md
%doc packaging/gpsd-nmea-bridge.service
%{_bindir}/openhop-geofence
%{_bindir}/openhop-geofence-trace
%{_unitdir}/openhop-geofence.service
%{_datadir}/polkit-1/rules.d/50-openhop-geofence-reboot.rules
%{_sysusersdir}/openhop-geofence.conf
%{_datadir}/openhop-geofence/
# The service edits config.toml in place (web interface), so it owns the file.
%dir %attr(2775,root,openhop-geofence) %{_sysconfdir}/openhop-geofence
%dir %attr(2755,openhop-geofence,openhop-geofence) %{_sysconfdir}/openhop-geofence/secrets
%config(noreplace) %attr(0664,openhop-geofence,openhop-geofence) %{_sysconfdir}/openhop-geofence/config.toml
%dir %attr(0750,openhop-geofence,openhop-geofence) %{_sharedstatedir}/openhop-geofence
%dir %attr(0750,openhop-geofence,openhop-geofence) %{_localstatedir}/cache/openhop-geofence

%changelog
* Tue Nov 24 2026 Edward Sandor <public+git@sandorlaboratories.com> - 0.1.0-1
- Initial release.
