/* Evaluation-only implementation of the existing duration Monte Carlo p50.
 * No fast-math: preserve CPython Random(0) draws and compensated float sums.
 */
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#include <math.h>

typedef struct { uint32_t state[624]; int index; } RNG;
static uint32_t next_word(RNG *r) {
    if (r->index >= 624) {
        for (int i=0;i<624;i++) {
            uint32_t y=(r->state[i]&0x80000000U)|(r->state[(i+1)%624]&0x7fffffffU);
            r->state[i]=r->state[(i+397)%624]^(y>>1)^((y&1U)?0x9908b0dfU:0);
        }
        r->index=0;
    }
    uint32_t y=r->state[r->index++];
    y^=y>>11; y^=(y<<7)&0x9d2c5680U; y^=(y<<15)&0xefc60000U; y^=y>>18;
    return y;
}
static int below(RNG *r, int n, int bits) {
    uint32_t k;
    do { k=next_word(r)>>(32-bits); } while (k>=(uint32_t)n);
    return (int)k;
}
static int compare_double(const void *a,const void *b) {
    double x=*(const double *)a,y=*(const double *)b;
    return (x>y)-(x<y);
}
double oracle_p50(const double **values,const int *sizes,int n,
                  const int *group_sizes,const int *members,int group_count,
                  const uint32_t *seed) {
    RNG r; memcpy(r.state,seed,624*sizeof(uint32_t)); r.index=(int)seed[624];
    double samples[2048],draw[n]; int bits[n];
    for(int i=0;i<n;i++){bits[i]=0; for(unsigned x=(unsigned)sizes[i];x;x>>=1)bits[i]++;}
    for(int sample=0;sample<2048;sample++) {
        for(int i=0;i<n;i++)draw[i]=values[i][below(&r,sizes[i],bits[i])];
        double hi=0,lo=0; int offset=0;
        for(int g=0;g<group_count;g++) {
            double x=draw[members[offset++]];
            for(int j=1;j<group_sizes[g];j++){double v=draw[members[offset++]]; if(v>x)x=v;}
            double next=hi+x;
            if(fabs(hi)>=fabs(x))lo+=(hi-next)+x;else lo+=(x-next)+hi;
            hi=next;
        }
        samples[sample]=hi+lo;
    }
    qsort(samples,2048,sizeof(double),compare_double);
    return (samples[1023]+samples[1024])/2.0;
}
