#import <AppKit/AppKit.h>
#import <stdint.h>

/// Qt exposes a QWidget WId as the native NSView on the Cocoa platform.
/// Keep the bridge deliberately tiny and ABI-safe: Python only calls this C
/// function, while all Objective-C messages are compiled by clang.
void dsh_apply_window_level(uintptr_t nativeView, int topmost) {
    @autoreleasepool {
        if (nativeView == 0) return;
        NSView *view = (__bridge NSView *)(void *)nativeView;
        NSWindow *window = view.window;
        if (window == nil) return;

        // Qt's translucent tool window otherwise gets AppKit's default drop
        // shadow.  When the window moves, that shadow can be composited from
        // the previous frame/position and look like a second black pet.
        // DSH draws its own card and sprite; the native window must have no
        // AppKit shadow or opaque backing color.
        window.hasShadow = NO;
        window.opaque = NO;
        window.backgroundColor = [NSColor clearColor];
        window.hidesOnDeactivate = NO;
        window.collectionBehavior = NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | (topmost ? NSWindowCollectionBehaviorFullScreenAuxiliary : 0);
        // Floating is the desktop-top layer intended for utility panels. The
        // previous status level made the pet behave like a system modal
        // surface on some macOS versions: it stayed the active/key window
        // above every app and users could no longer type elsewhere.
        window.level = topmost ? NSFloatingWindowLevel : NSNormalWindowLevel;
        // Do not orderFrontRegardless here. This bridge is called by the
        // periodic stack watchdog; forcing the panel front every two seconds
        // activates/steals keyboard focus from the user's current app. The
        // initial/menu transition explicitly orders the window when needed.
    }
}
