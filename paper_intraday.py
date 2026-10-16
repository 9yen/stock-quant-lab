import json
from quant_lab.paper_execution import main

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps(dict(status='STOP', error_type=type(error).__name__)), flush=True)
        raise SystemExit(1)
