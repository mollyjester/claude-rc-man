# What the tools' podman and docker say of a port a container or a pod is asked to publish,
# sourced by them (podman) with their arguments where the image's Podman runs. A container
# shares the workspace's network (containers.conf netns), so publishing gives it nothing: this
# says so on standard error and changes nothing, and Podman runs as asked. Nothing is said where
# --network or --net names a network other than the host's, or --pod or --pod-id-file a pod. It
# uses the shell's built-ins alone, and the wrapper's NAME, VALUED and say. The variables it sets
# are its PUBLISH_ lists and, in publishing_explained, shares, published, all, elsewhere, arg,
# option, value, letters, letter and flag, none of which the wrapper uses.
#
# The options run, create, container run, container create and pod create take, as Podman's
# help lists them (tests/fakes/podman-help, recorded from the default image's Podman, which
# tests/test_podman_wrapper.py holds these lists to): those taking a value, by name, with --net,
# the alias of --network its help does not show; the letters of those taking a value; the
# letters of those taking none; and the letters of Podman's global options, which it takes
# after the subcommand too, that no subcommand here takes for one of its own, all taking none.
PUBLISH_VALUED=" --add-host --annotation --arch --attach --authfile --blkio-weight \
--blkio-weight-device --cap-add --cap-drop --cgroup-conf --cgroup-parent --cgroupns --cgroups \
--chrootdirs --cidfile --conmon-pidfile --cpu-period --cpu-quota --cpu-rt-period \
--cpu-rt-runtime --cpu-shares --cpus --cpuset-cpus --cpuset-mems --decryption-key --detach-keys \
--device --device-cgroup-rule --device-read-bps --device-read-iops --device-write-bps \
--device-write-iops --dns --dns-option --dns-search --entrypoint --env --env-file --env-merge \
--exit-policy --expose --gidmap --group-add --group-entry --health-cmd --health-interval \
--health-on-failure --health-retries --health-start-period --health-startup-cmd \
--health-startup-interval --health-startup-retries --health-startup-success \
--health-startup-timeout --health-timeout --hostname --hostuser --image-volume --infra-command \
--infra-conmon-pidfile --infra-image --infra-name --init-ctr --init-path --ip --ip6 --ipc \
--label --label-file --log-driver --log-opt --mac-address --memory --memory-reservation \
--memory-swap --memory-swappiness --mount --name --net --network --network-alias \
--oom-score-adj --os --passwd-entry --personality --pid --pidfile --pids-limit --platform --pod \
--pod-id-file --preserve-fds --publish --pull --rdt-class --requires --restart --sdnotify \
--seccomp-policy --secret --security-opt --share --shm-size --shm-size-systemd --stop-signal \
--stop-timeout --subgidname --subuidname --sysctl --systemd --timeout --tmpfs --tz --uidmap \
--ulimit --umask --unsetenv --user --userns --uts --variant --volume --volumes-from --workdir "
PUBLISH_VALUED_LETTERS=acehlmnpuvw
PUBLISH_LETTERS=Pdiqt
PUBLISH_GLOBAL_LETTERS=r

# Whether a value given to -P or --publish-all, empty where none is, turns it on.
publishes_all() {
	case "$1" in
	0 | f | F | false | FALSE | False) return 1 ;;
	esac
}

# The arguments are read as Podman's option parser reads them: the global options (the wrapper's
# VALUED) and the subcommand, then its options, among which the global ones may come too, up to
# the first that is not one, the image or the pod's name, or up to --. A cluster of letters
# (-dp 80:80) is read a letter at a time, a letter taking a value takes the rest of the cluster
# or the next argument, and one taking none takes what follows an = (-P=false); a letter that is
# no option of these stops the reading, as Podman stops there and says so itself.
publishing_explained() {
	while [ $# -gt 0 ]; do
		case "$1" in
		--) return 0 ;;
		--*=*) shift ;;
		-*)
			case "$VALUED" in
			*" $1 "*) [ $# -ge 2 ] && shift 2 || return 0 ;;
			*) shift ;;
			esac
			;;
		*) break ;;
		esac
	done
	shares="a container"
	case "${1-} ${2-}" in
	"run "* | "create "*) shift ;;
	"container run" | "container create") shift 2 ;;
	"pod create")
		shares="a pod"
		shift 2
		;;
	*) return 0 ;;
	esac
	published=
	all=
	elsewhere=
	while [ $# -gt 0 ]; do
		arg=$1
		shift
		case "$arg" in
		--) break ;;
		--*)
			option=${arg%%=*}
			value=
			if [ "$option" != "$arg" ]; then
				value=${arg#*=}
			else
				case "$PUBLISH_VALUED$VALUED" in
				*" $option "*)
					[ $# -gt 0 ] || break
					value=$1
					shift
					;;
				esac
			fi
			case "$option" in
			--publish) published=${published:-$option} ;;
			--publish-all) if publishes_all "$value"; then all=$option; else all=; fi ;;
			--network | --net | --pod) [ "$value" = host ] || elsewhere=1 ;;
			--pod-id-file) elsewhere=1 ;;
			esac
			;;
		-?*)
			letters=${arg#-}
			while [ -n "$letters" ]; do
				letter=${letters%"${letters#?}"}
				letters=${letters#?}
				case "$PUBLISH_LETTERS$PUBLISH_GLOBAL_LETTERS" in
				*"$letter"*)
					value=
					case "$letters" in
					=*)
						value=${letters#=}
						letters=
						;;
					esac
					if [ "$letter" = P ]; then
						if publishes_all "$value"; then all=-P; else all=; fi
					fi
					continue
					;;
				esac
				case "$PUBLISH_VALUED_LETTERS" in
				*"$letter"*) ;;
				*) return 0 ;;
				esac
				if [ -n "$letters" ]; then
					letters=
				elif [ $# -gt 0 ]; then
					shift
				fi
				[ "$letter" = p ] && published=${published:--p}
			done
			;;
		*) break ;;
		esac
	done
	flag=${published:-$all}
	[ -n "$flag" ] && [ -z "$elsewhere" ] || return 0
	say "$flag publishes no port here: $shares shares this workspace's network, so what it listens on is on the workspace's own ports; reach it at localhost:<the port it listens on>"
}

publishing_explained "$@"
