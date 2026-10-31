"""Discover bounded official program detail URLs from a stored list snapshot."""
import argparse
import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from .catalog import ROOT

LIST_URL = 'https://bokji.gangnam.go.kr/board/BBS_NEWS/list.do?mid=ID01_02'


class ProgramLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.current, self.parts, self.links = None, [], {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a':
            url = urljoin(LIST_URL, attrs.get('href', ''))
            parts = urlsplit(url)
            if parts.scheme == 'https' and parts.netloc == 'bokji.gangnam.go.kr' and re.fullmatch(r'/board/BBS_NEWS/\d+/view.do', parts.path):
                self.current, self.parts = 'https://bokji.gangnam.go.kr'+parts.path+'?mid=ID01_02', []
        if tag == 'img' and self.current:
            self.parts.append(attrs.get('alt', ''))

    def handle_data(self, text):
        if self.current:
            self.parts.append(text)

    def handle_endtag(self, tag):
        if tag == 'a' and self.current:
            title = re.sub(r'\s+', ' ', ''.join(self.parts)).strip()
            if title and (self.current not in self.links or len(title) < len(self.links[self.current])):
                self.links[self.current] = title
            self.current = None


def discover(root=ROOT, register_limit=0):
    manifest = json.loads((root/'data/manifest.json').read_text())
    listing = next(row for row in manifest if row['id']=='gangnam_discovery' and row['status']=='collected')
    parser = ProgramLinks();parser.feed((root/listing['raw_path']).read_text())
    sources = json.loads((root/'data/sources.json').read_text())
    known = {row['url'] for row in sources}
    queue=[]
    for url,title in list(parser.links.items())[:50]:
        queue.append({'url':url,'title':title,'registered':url in known,'state':'항목 매핑·이용조건 검토 필요'})
    added=0
    for row in queue:
        if not row['registered'] and added < register_limit:
            number = re.search(r'/BBS_NEWS/(\d+)/',row['url'])[1]
            sources.append({'id':'gangnam_discovered_'+number,'title':row['title'],'url':row['url'],
                            'publisher':'강남구 스마트복지관','role':'discovered_review_pending','max_age_days':3})
            row['registered']=True;added+=1
    if added:
        (root/'data/sources.json').write_text(json.dumps(sources,ensure_ascii=False,indent=2))
    result={'list_source':listing['url'],'list_sha256':listing['sha256'],'candidates':queue,'added_sources':added,
            'promotion':'발견한 공고는 자동 추천하지 않음. 수집·근거·필드 검증을 통과한 seed만 추천 후보가 됨.'}
    (root/'data/processed/discovery.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--register',type=int,default=0,choices=range(0,21));args=parser.parse_args()
    result=discover(register_limit=args.register)
    print(json.dumps({'discovered':len(result['candidates']),'added_sources':result['added_sources']},ensure_ascii=False))
