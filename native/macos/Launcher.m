#import <AppKit/AppKit.h>
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

// Finder launches do not provide a useful stdin stream. Keep one end of a
// private pipe open so the native helper uses EOF as the app lifecycle signal
// without exiting immediately when DSH.app is opened from Finder.
int main(int argc, char **argv) {
    @autoreleasepool {
        int lifecycle[2] = {-1, -1};
        // Finder launches have no useful stdin, so keep a private pipe open
        // as the app-lifetime signal. Direct diagnostic/protocol invocations
        // pass their real stdin through so --headless smoke tests can talk to
        // the same bundle executable.
        const int finder_launch = (argc <= 1);
        if (finder_launch && pipe(lifecycle) != 0) return 2;

        char executable[PATH_MAX];
        uint32_t size = (uint32_t)sizeof(executable);
        if (_NSGetExecutablePath(executable, &size) != 0) return 2;
        char resolved[PATH_MAX];
        if (realpath(executable, resolved) != NULL) {
            strncpy(executable, resolved, sizeof(executable) - 1);
            executable[sizeof(executable) - 1] = '\0';
        }
        char directory[PATH_MAX];
        strncpy(directory, executable, sizeof(directory) - 1);
        directory[sizeof(directory) - 1] = '\0';
        char *macos = dirname(directory);
        char helper[PATH_MAX];
        snprintf(helper, sizeof(helper), "%s/../Resources/dsh-dafeiyu-helper", macos);
        if (access(helper, X_OK) != 0) return 3;

        if (finder_launch) {
            if (dup2(lifecycle[0], STDIN_FILENO) < 0) return 2;
            close(lifecycle[0]);
        }
        setenv("LANG", "en_US.UTF-8", 1);
        setenv("LC_ALL", "en_US.UTF-8", 1);
        // Keep Finder launches argument-free, but pass protocol diagnostics
        // such as --headless/--snapshot through when CI or the host invokes
        // the bundle executable directly.
        char **child_argv = calloc((size_t)argc + 1, sizeof(char *));
        if (child_argv == NULL) return 2;
        child_argv[0] = helper;
        for (int index = 1; index < argc; index++) child_argv[index] = argv[index];
        child_argv[argc] = NULL;
        execv(helper, child_argv);
        free(child_argv);
        return 127;
    }
}
