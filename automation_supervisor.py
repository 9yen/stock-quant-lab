"""Keep configured local paper workers available; never remove stop flags."""
if __name__ == '__main__':
    from pathlib import Path
    from quant_lab.automation import run_supervisor
    run_supervisor(Path(__file__).resolve().parent)
