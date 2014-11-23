#
# Regular cron jobs for the synapse package
#
0 4	* * *	root	[ -x /usr/bin/synapse_maintenance ] && /usr/bin/synapse_maintenance
