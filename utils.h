#define ABS(a) (((a) < 0) ? -(a) : (a))
#ifndef MIN
#define MIN(a, b) (((a) < (b)) ? (a) : (b))
#endif
#ifndef MAX
#define MAX(a, b) (((a) > (b)) ? (a) : (b))
#endif
#define LENGTH_OF(x) (sizeof(x)/sizeof(x[0]))

static inline void reset_getopt(void)
{
#ifdef __GLIBC__
    optind = 0;
#else
    optind = 1;
#endif

#ifdef HAVE_OPTRESET
    optreset = 1;
#endif
}

void daemon_mode(void);

#define OPTION_INT(idx, v) \
  case idx: \
    DBG("case " #idx); \
    if (sscanf(optarg, "%d", &settings->v) != 1) { \
        fprintf(stderr, "Invalid value '%s' for -" #v " (integer required)\n", optarg); \
        exit(EXIT_FAILURE); \
    } \
    settings->v##_set = 1;

#define OPTION_INT_AUTO(idx, v) \
  case idx: \
    DBG("case " #idx); \
    if (strcasecmp("auto", optarg) == 0) { \
        settings->v##_auto = 1; \
    } else if (sscanf(optarg, "%d", &settings->v) != 1) { \
        fprintf(stderr, "Invalid value '%s' for -" #v " (auto or integer required)\n", optarg); \
        exit(EXIT_FAILURE); \
    } \
    settings->v##_set = 1;

/* 1 is true, 0 is false */
#define OPTION_BOOL(idx, v) \
  case idx: \
    DBG("case " #idx); \
    if (strcasecmp("true", optarg) == 0) { \
        settings->v = 1; \
    } else if (strcasecmp("false", optarg) == 0) { \
        settings->v = 0; \
    } else { \
        fprintf(stderr, "Invalid value '%s' for -" #v " (true/false accepted)\n", optarg); \
        exit(EXIT_FAILURE); \
    } \
    settings->v##_set = 1;

/* table must be defined as array of structs with 'k', 'v' strings */
#define OPTION_MULTI(idx, var, table) \
  case idx: \
    DBG("case " #idx); \
    settings->var##_set = 0; \
    for(i = 0; i < LENGTH_OF(table); i++) { \
        if(strcasecmp(table[i].k, optarg) == 0) { \
            settings->var = table[i].v; \
            settings->var##_set = 1; \
            break; \
        } \
    } \
    if (settings->var##_set == 0) { \
        fprintf(stderr, "Invalid value '%s' for -" #var "\n", optarg); \
        exit(EXIT_FAILURE); \
    }
    
#define OPTION_MULTI_OR_INT(idx, var1, var1_default, var2, table) \
  case idx: \
    DBG("case " #idx); \
    settings->var1 = var1_default; \
    for(i = 0; i < LENGTH_OF(table); i++) { \
        if(strcasecmp(table[i].k, optarg) == 0) { \
            settings->var1 = table[i].v; \
            break; \
        } \
    } \
    if (settings->var1 == var1_default) { \
        if (sscanf(optarg, "%d", &settings->var2) != 1) { \
            fprintf(stderr, "Invalid value '%s' for -" #var2 "\n", optarg); \
            exit(EXIT_FAILURE); \
        } \
    } \
    settings->var2##_set = 1;

void resolutions_help(const char * padding);
void parse_resolution_opt(const char * optarg, int * width, int * height);

