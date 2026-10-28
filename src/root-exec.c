/*
 * root-exec: the image starts as dev so `docker exec` lands in dev.
 *
 * This setuid wrapper calls setuid(0) in place and then execs the entrypoint,
 * keeping /init as PID 1. sudo is unusable here: it forks, which would take PID 1
 * away from s6-overlay and break signal forwarding and zombie reaping.
 */
#include <grp.h>
#include <stdio.h>
#include <unistd.h>

int main(int argc, char **argv)
{
    (void)argc;

    if (setgroups(0, 0) != 0 || setgid(0) != 0 || setuid(0) != 0) {
        perror("root-exec");
        return 1;
    }

    execv("/usr/local/bin/entrypoint.sh", argv);
    perror("root-exec");
    return 1;
}
