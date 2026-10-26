/*
 * insightful-proxy.c
 *
 * LD_PRELOAD library injected into Workpuls (Electron, XWayland).
 *
 * Intercepts X11 calls that fail under XWayland/Hyprland and returns
 * data from shared memory written by insightful-daemon.py on the host.
 *
 * APM (activity tracking) is now handled by a real uinput virtual device
 * created by the daemon - tracker.node reads it natively.
 *
 * SHM layout (matches Python struct.pack("<qii256s256s", ...)):
 *   int64_t  last_activity   Unix timestamp of last activity
 *   int32_t  idle_ms         reserved (0)
 *   int32_t  force_window    always 1
 *   char     window_title[256]
 *   char     window_class[256]
 *
 * Intercepted calls:
 *   1. XScreenSaverQueryInfo  idle time from SHM
 *   2. XGetInputFocus         return our synthetic window
 *   3. XGetWindowProperty     _NET_ACTIVE_WINDOW, WM_NAME, WM_CLASS, _NET_WM_NAME
 *   4. XGetClassHint          class from SHM
 *   5. XGetWMName             title from SHM
 *   6. XGetWindowAttributes   force map_state = IsViewable
 *   7. XQueryPointer          always return valid data
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <stdint.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <X11/Xlib.h>
#include <X11/Xatom.h>
#include <X11/Xutil.h>

/* ------------------------------------------------------------------ */
/*  X11 error handler -- suppress BadWindow and other non-fatal errors  */
/* ------------------------------------------------------------------ */
static int x11_error_handler(Display *dpy, XErrorEvent *event)
{
    /* Silently swallow BadWindow, BadDrawable, BadMatch, BadAccess.
       These are expected: our synthetic window ID is used by X11 calls
       we don't intercept (e.g. XChangeProperty, XSelectInput). */
    if (event->error_code == BadWindow  ||
        event->error_code == BadDrawable ||
        event->error_code == BadMatch   ||
        event->error_code == BadAccess)
        return 0;
    /* For anything else, print a one-line warning but don't crash. */
    return 0;
}

static void __attribute__((constructor)) install_error_handler(void)
{
    XSetErrorHandler(x11_error_handler);
}

/* ------------------------------------------------------------------ */
/*  Shared memory                                                       */
/* ------------------------------------------------------------------ */
#define SHM_NAME  "/insightful_state"
#define SHM_SIZE  1024

typedef struct {
    volatile int64_t last_activity;
    volatile int32_t idle_ms;
    volatile int32_t force_window;
    volatile char    window_title[256];
    volatile char    window_class[256];
} shm_state_t;

static shm_state_t *shm    = NULL;
static int          shm_fd = -1;

static void connect_shm(void)
{
    if (shm) return;
    shm_fd = shm_open(SHM_NAME, O_RDONLY, 0666);
    if (shm_fd < 0) return;
    shm = mmap(NULL, SHM_SIZE, PROT_READ, MAP_SHARED, shm_fd, 0);
    if (shm == MAP_FAILED) { shm = NULL; close(shm_fd); shm_fd = -1; }
}

/* ------------------------------------------------------------------ */
/*  X11 atoms and window state                                         */
/* ------------------------------------------------------------------ */

/* XScreenSaverInfo -- defined locally to avoid including scrnsaver.h  */
typedef struct {
    Window        window;
    int           state;
    int           kind;
    unsigned long til_or_since;
    unsigned long idle;
    unsigned long eventMask;
} XScreenSaverInfo_t;

static Atom   WM_NAME_atom        = 0;
static Atom   WM_CLASS_atom       = 0;
static Atom   UTF8_STRING_atom    = 0;
static Atom   NET_WM_NAME_atom    = 0;
static Atom   NET_ACTIVE_WIN_atom = 0;
static Window cached_window       = None;

/* ------------------------------------------------------------------ */
/*  Real function pointers                                             */
/* ------------------------------------------------------------------ */
static XScreenSaverInfo_t *(*real_XScreenSaverAllocInfo)(void)         = NULL;
static int (*real_XScreenSaverQueryInfo)(Display*,Drawable,void*)      = NULL;
static int (*real_XGetInputFocus)(Display*,Window*,int*)               = NULL;
static int (*real_XGetWindowProperty)(Display*,Window,Atom,long,long,
              Bool,Atom,Atom*,int*,unsigned long*,unsigned long*,
              unsigned char**)                                          = NULL;
static int (*real_XGetClassHint)(Display*,Window,XClassHint*)          = NULL;
static int (*real_XGetWMName)(Display*,Window,XTextProperty*)          = NULL;
static int (*real_XGetWindowAttributes)(Display*,Window,
              XWindowAttributes*)                                       = NULL;
static Bool (*real_XQueryPointer)(Display*,Window,Window*,Window*,
              int*,int*,int*,int*,unsigned int*)                        = NULL;

static void init_real(void)
{
    static int done = 0;
    if (done) return;
    real_XScreenSaverAllocInfo  = dlsym(RTLD_NEXT, "XScreenSaverAllocInfo");
    real_XScreenSaverQueryInfo  = dlsym(RTLD_NEXT, "XScreenSaverQueryInfo");
    real_XGetInputFocus         = dlsym(RTLD_NEXT, "XGetInputFocus");
    real_XGetWindowProperty     = dlsym(RTLD_NEXT, "XGetWindowProperty");
    real_XGetClassHint          = dlsym(RTLD_NEXT, "XGetClassHint");
    real_XGetWMName             = dlsym(RTLD_NEXT, "XGetWMName");
    real_XGetWindowAttributes   = dlsym(RTLD_NEXT, "XGetWindowAttributes");
    real_XQueryPointer          = dlsym(RTLD_NEXT, "XQueryPointer");
    done = 1;
}

static void init_atoms(Display *dpy)
{
    if (!dpy || WM_NAME_atom) return;
    WM_NAME_atom        = XInternAtom(dpy, "WM_NAME",            False);
    WM_CLASS_atom       = XInternAtom(dpy, "WM_CLASS",           False);
    UTF8_STRING_atom    = XInternAtom(dpy, "UTF8_STRING",        False);
    NET_WM_NAME_atom    = XInternAtom(dpy, "_NET_WM_NAME",       False);
    NET_ACTIVE_WIN_atom = XInternAtom(dpy, "_NET_ACTIVE_WINDOW", False);
}

static Window get_or_create_window(Display *dpy)
{
    if (cached_window == None && dpy) {
        int screen = DefaultScreen(dpy);
        cached_window = XCreateSimpleWindow(
            dpy, RootWindow(dpy, screen),
            0, 0, 1, 1, 0,
            BlackPixel(dpy, screen),
            BlackPixel(dpy, screen));
        if (cached_window != None) {
            XStoreName(dpy, cached_window, "Workpuls");
            XFlush(dpy);
        }
    }
    return cached_window;
}

/* ------------------------------------------------------------------ */
/*  XScreenSaverAllocInfo                                              */
/* ------------------------------------------------------------------ */
XScreenSaverInfo_t *XScreenSaverAllocInfo(void)
{
    init_real();
    if (real_XScreenSaverAllocInfo) return real_XScreenSaverAllocInfo();
    return calloc(1, sizeof(XScreenSaverInfo_t));
}

/* ------------------------------------------------------------------ */
/*  XScreenSaverQueryInfo  -- idle time from SHM                       */
/* ------------------------------------------------------------------ */
int XScreenSaverQueryInfo(Display *dpy, Drawable drawable, XScreenSaverInfo_t *info)
{
    init_real();
    connect_shm();

    if (shm && shm->last_activity > 0) {
        time_t  now  = time(NULL);
        int64_t diff = (int64_t)now - (int64_t)shm->last_activity;
        if (diff < 0) diff = 0;
        info->idle  = (unsigned long)(diff * 1000);
        info->state = 0;
        info->kind  = 0;
        return 1;
    }

    if (real_XScreenSaverQueryInfo) {
        int r = real_XScreenSaverQueryInfo(dpy, drawable, (void *)info);
        if (r) return r;
    }
    info->idle  = 0;
    info->state = 0;
    return 1;
}

/* ------------------------------------------------------------------ */
/*  XGetInputFocus  -- return our synthetic window                     */
/* ------------------------------------------------------------------ */
int XGetInputFocus(Display *dpy, Window *focus_return, int *revert_to_return)
{
    init_real();
    connect_shm();

    if (shm && shm->force_window) {
        Window w = get_or_create_window(dpy);
        if (w != None) {
            *focus_return     = w;
            *revert_to_return = RevertToParent;
            return 1;
        }
    }

    if (real_XGetInputFocus)
        return real_XGetInputFocus(dpy, focus_return, revert_to_return);

    *focus_return     = None;
    *revert_to_return = RevertToNone;
    return 1;
}

/* ------------------------------------------------------------------ */
/*  XGetWindowProperty                                                 */
/* ------------------------------------------------------------------ */
int XGetWindowProperty(Display *dpy, Window w, Atom property,
                       long long_offset, long long_length, Bool delete,
                       Atom req_type,
                       Atom *actual_type_return,
                       int  *actual_format_return,
                       unsigned long *nitems_return,
                       unsigned long *bytes_after_return,
                       unsigned char **prop_return)
{
    init_real();
    connect_shm();
    init_atoms(dpy);

    if (shm && shm->force_window && shm->window_title[0] != '\0') {

        /* _NET_ACTIVE_WINDOW on root: return our fake window ID */
        if (property == NET_ACTIVE_WIN_atom) {
            Window fw = get_or_create_window(dpy);
            if (fw != None) {
                Window *buf = malloc(sizeof(Window));
                if (buf) *buf = fw;
                if (actual_type_return)   *actual_type_return   = XA_WINDOW;
                if (actual_format_return) *actual_format_return = 32;
                if (nitems_return)        *nitems_return        = 1;
                if (bytes_after_return)   *bytes_after_return   = 0;
                if (prop_return)          *prop_return          = (unsigned char *)buf;
                return Success;
            }
        }

        /* WM_NAME / _NET_WM_NAME: window title */
        if (property == WM_NAME_atom || property == NET_WM_NAME_atom) {
            const char *t   = (const char *)shm->window_title;
            size_t      len = strlen(t);
            if (actual_type_return)   *actual_type_return   = UTF8_STRING_atom ? UTF8_STRING_atom : XA_STRING;
            if (actual_format_return) *actual_format_return = 8;
            if (nitems_return)        *nitems_return        = len;
            if (bytes_after_return)   *bytes_after_return   = 0;
            if (prop_return)          *prop_return          = (unsigned char *)strdup(t);
            return Success;
        }

        /* WM_CLASS: "instance\0class\0" */
        if (property == WM_CLASS_atom) {
            const char *cls    = (const char *)shm->window_class;
            size_t      len    = strlen(cls);
            size_t      buflen = len + 1 + len + 1;
            char *buf = malloc(buflen);
            if (buf) {
                memcpy(buf,           cls, len + 1);
                memcpy(buf + len + 1, cls, len + 1);
            }
            if (actual_type_return)   *actual_type_return   = XA_STRING;
            if (actual_format_return) *actual_format_return = 8;
            if (nitems_return)        *nitems_return        = buflen;
            if (bytes_after_return)   *bytes_after_return   = 0;
            if (prop_return)          *prop_return          = (unsigned char *)buf;
            return Success;
        }
    }

    if (real_XGetWindowProperty)
        return real_XGetWindowProperty(dpy, w, property, long_offset,
                                       long_length, delete, req_type,
                                       actual_type_return, actual_format_return,
                                       nitems_return, bytes_after_return,
                                       prop_return);
    return BadAtom;
}

/* ------------------------------------------------------------------ */
/*  XGetClassHint                                                      */
/* ------------------------------------------------------------------ */
int XGetClassHint(Display *dpy, Window w, XClassHint *class_hints_return)
{
    init_real();
    connect_shm();

    if (shm && shm->force_window && shm->window_class[0] != '\0') {
        const char *cls = (const char *)shm->window_class;
        class_hints_return->res_name  = strdup(cls);
        class_hints_return->res_class = strdup(cls);
        return 1;
    }

    if (real_XGetClassHint)
        return real_XGetClassHint(dpy, w, class_hints_return);
    return 0;
}

/* ------------------------------------------------------------------ */
/*  XGetWMName                                                         */
/* ------------------------------------------------------------------ */
int XGetWMName(Display *dpy, Window w, XTextProperty *text_prop_return)
{
    init_real();
    connect_shm();

    if (shm && shm->force_window && shm->window_title[0] != '\0') {
        const char *t = (const char *)shm->window_title;
        text_prop_return->value    = (unsigned char *)strdup(t);
        text_prop_return->encoding = XA_STRING;
        text_prop_return->format   = 8;
        text_prop_return->nitems   = strlen(t);
        return 1;
    }

    if (real_XGetWMName)
        return real_XGetWMName(dpy, w, text_prop_return);
    return 0;
}

/* ------------------------------------------------------------------ */
/*  XGetWindowAttributes  -- force IsViewable                         */
/* ------------------------------------------------------------------ */
int XGetWindowAttributes(Display *dpy, Window w, XWindowAttributes *wa)
{
    init_real();

    if (real_XGetWindowAttributes) {
        int r = real_XGetWindowAttributes(dpy, w, wa);
        if (r) {
            wa->map_state = IsViewable;
            return r;
        }
    }

    if (wa) {
        memset(wa, 0, sizeof(*wa));
        wa->width     = 1920;
        wa->height    = 1080;
        wa->map_state = IsViewable;
        wa->class     = InputOutput;
    }
    return 1;
}

/* ------------------------------------------------------------------ */
/*  XQueryPointer  -- always return valid data                         */
/* ------------------------------------------------------------------ */
Bool XQueryPointer(Display *dpy, Window w,
                   Window *root_return, Window *child_return,
                   int *root_x_return, int *root_y_return,
                   int *win_x_return,  int *win_y_return,
                   unsigned int *mask_return)
{
    init_real();

    if (real_XQueryPointer) {
        Bool r = real_XQueryPointer(dpy, w, root_return, child_return,
                                    root_x_return, root_y_return,
                                    win_x_return, win_y_return, mask_return);
        if (r) return r;
    }

    if (root_return)   *root_return   = DefaultRootWindow(dpy);
    if (child_return)  *child_return  = None;
    if (root_x_return) *root_x_return = 0;
    if (root_y_return) *root_y_return = 0;
    if (win_x_return)  *win_x_return  = 0;
    if (win_y_return)  *win_y_return  = 0;
    if (mask_return)   *mask_return   = 0;
    return True;
}
