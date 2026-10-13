"""Publication-exportable static figures from measured, fixed-common-set results."""
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from research.kb_systematic_20261013.study import HERE, read

def main():
    dest=HERE/'figures';dest.mkdir(exist_ok=True)
    data=read(HERE/'results/calibration-common.json')['metrics']
    fig,axes=plt.subplots(1,2,figsize=(10,3.7),layout='constrained')
    for name,marker in [('native','o'),('uniform','x'),('semantic','^')]:
        x=[0,5,20,100]
        axes[0].plot(x,[100*data[f'{name}-{n}']['accuracy'] for n in x],marker=marker,label=name,alpha=.8)
        axes[1].plot(x,[data[f'{name}-{n}']['log_mae'] for n in x],marker=marker,label=name,alpha=.8)
    for ax in axes:
        ax.set_xlabel('Target feedback clause events');ax.grid(alpha=.2);ax.legend(frameon=False)
    axes[0].set_ylabel('Time-bin accuracy (%)');axes[1].set_ylabel('Log MAE (seconds, log1p)')
    fig.suptitle('Fixed common set: 2,046 calls / 60 target tasks; one feedback pool')
    fig.savefig(dest/'feedback-curve.png',dpi=180);fig.savefig(dest/'feedback-curve.pdf');plt.close(fig)

if __name__=='__main__':main()
