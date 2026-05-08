#ifndef MJPG_STREAMER_H
#define MJPG_STREAMER_H
#define SOURCE_VERSION "2.0"

#define MAX_INPUT_PLUGINS 10
#define MAX_OUTPUT_PLUGINS 10
#define MAX_PLUGIN_ARGUMENTS 32

#include <linux/types.h>          /* for videodev2.h */
#include <linux/videodev2.h>
#include <pthread.h>

#ifdef DEBUG
#define DBG(...) fprintf(stderr, " DBG(%s, %s(), %d): ", __FILE__, __FUNCTION__, __LINE__); fprintf(stderr, __VA_ARGS__)
#else
#define DBG(...)
#endif

#define LOG(...) { char _bf[1024] = {0}; snprintf(_bf, sizeof(_bf)-1, __VA_ARGS__); fprintf(stderr, "%s", _bf); syslog(LOG_INFO, "%s", _bf); }

#include "plugins/input.h"
#include "plugins/output.h"

typedef struct _globals globals;

typedef enum {
    Dest_Input = 0,
    Dest_Output = 1,
    Dest_Program = 2,
} command_dest;

enum _cmd_group {
    IN_CMD_GENERIC =        0,
    IN_CMD_V4L2 =           1,
    IN_CMD_RESOLUTION =     2,
    IN_CMD_JPEG_QUALITY =   3,
    IN_CMD_PWC =            4,
};

typedef struct _control control;
struct _control {
    struct v4l2_queryctrl ctrl;
    int value;
    struct v4l2_querymenu *menuitems;
    int class_id;
    int group;
};

struct _globals {
    int stop;

    input in[MAX_INPUT_PLUGINS];
    int incnt;

    output out[MAX_OUTPUT_PLUGINS];
    int outcnt;

};

#endif
