from pathlib import Path

from simple_bot import replace_pairs

CORPUS_PATH = Path('db.txt')
DATABASE_PATH = Path('db.sqlite3')


def load_corpus_pairs(path: Path) -> list[tuple[str, str]]:
    lines = [
        line.strip()
        for line in path.read_text(encoding='utf-8').splitlines()
        if line.strip() and set(line.strip()) != {'-'}
    ]

    if lines and lines[0].startswith('训练前需将语料重命名为'):
        raise ValueError('db.txt 还是模板内容，请先替换成你自己的问答语料。')

    if len(lines) % 2 != 0:
        raise ValueError('db.txt 格式错误：每个问题都必须紧跟一行回答。')

    return [(lines[index], lines[index + 1]) for index in range(0, len(lines), 2)]


def main() -> int:
    try:
        pairs = load_corpus_pairs(CORPUS_PATH)
    except FileNotFoundError:
        print('未找到 db.txt，请先创建语料文件。')
        return 1
    except ValueError as error:
        print(error)
        return 1

    total = replace_pairs(DATABASE_PATH, pairs)
    print(f'训练完毕，共写入 {total} 组问答到 {DATABASE_PATH.name}。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
