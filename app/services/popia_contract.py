"""Downloadable operator contract with ordinary, explicitly intentional PDF e-signatures.

No signatures are fabricated, no acceptance is recorded by download, and no identity
verification or cryptographic/advanced electronic signature is claimed.
"""
import hashlib
from pathlib import Path
from reportlab.lib import colors
from reportlab.platypus import Flowable, Paragraph
from reportlab.lib.styles import ParagraphStyle

CONTRACT_PATH = Path(__file__).resolve().parents[2] / 'docs' / 'popia' / 'SANO-JACKAPP-OPERATOR-CONTRACT.md'
PARTIES = [('sano', 'Sano Trailers', ''), ('jackapp', 'Jackapp', 'Donovan Jackson t/a Jackapp')]
FIELD_ROWS = [
    ('legal_name', 'Full legal name of the party', 520, 160),
    ('registration', 'Registration number / legal form', 468, 80),
    ('representative', 'Authorised representative — full name', 416, 100),
    ('capacity', 'Position / signing capacity', 364, 80),
    ('signed_date', 'Date signed (DD-MM-YYYY)', 312, 10),
    ('electronic_signature', 'Electronic signature — type full name', 260, 100),
]


def contract_text():
    return CONTRACT_PATH.read_text(encoding='utf-8')


def signing_field_spec():
    """One-page relative-coordinate layout spec, also used to build the real widgets."""
    fields = []
    for index, (prefix, party, legal_name) in enumerate(PARTIES):
        x = index * 259
        for suffix, label, y, max_length in FIELD_ROWS:
            value = legal_name if suffix == 'legal_name' else ('2018/521057/07' if prefix == 'sano' and suffix == 'registration' else ('Sole proprietor' if prefix == 'jackapp' and suffix == 'registration' else ''))
            fields.append({'name':prefix+'_'+suffix,'type':'text','page':1,'label':label,
                           'label_box':[x,y+26,x+225,y+38],'entry_box':[x,y,x+225,y+24],
                           'value':value,'max_length':max_length,'required':suffix!='registration'})
        fields.append({'name':prefix+'_signing_declaration','type':'checkbox','page':1,
                       'label':party+' signing declaration','label_box':[x+20,210,x+225,230],
                       'entry_box':[x,214,x+14,228],'checked':False,'required':True})
    return {'title':'Sano–Jackapp electronic signing page','page_size':'A4','fields':fields}


class ContractSigningPage(Flowable):
    def __init__(self, text):
        super().__init__()
        self.width = 499
        self.height = 720
        self.digest = hashlib.sha256(text.encode('utf-8')).hexdigest()

    def draw(self):
        c = self.canv
        ink = colors.HexColor('#174d36')
        c.setFillColor(ink); c.setFont('Helvetica-Bold',16)
        c.drawString(0,698,'Electronic signing — both parties')
        style = ParagraphStyle('SigningHelp',fontName='Helvetica',fontSize=9,leading=12)
        def paragraph(text,x,y,width=489):
            p=Paragraph(text,style); _,height=p.wrap(width,100)
            p.drawOn(c,x,y-height)
        paragraph('Contract version 1.0. Confirm the legal party details, complete the fields, '
                  'and deliberately sign below. Nothing is pre-signed. Use Adobe Acrobat Reader '
                  'or another compatible PDF editor; a browser preview may not support form saving.',0,680)
        for index, (prefix, party, legal_name) in enumerate(PARTIES):
            c.setFont('Helvetica-Bold',12);c.setFillColor(ink)
            c.drawString(index*259,588,party)
        c.setFillColor(colors.HexColor('#243041'))
        for field in signing_field_spec()['fields']:
            x,y,right,top=field['entry_box']
            if field['type']=='text':
                c.setFont('Helvetica',8.5)
                c.drawString(field['label_box'][0],field['label_box'][1]+2,field['label'].replace('—','-'))
                c.acroForm.textfield(name=field['name'],tooltip=field['label'],x=x,y=y,
                                    width=right-x,height=top-y,value=field['value'],maxlen=field['max_length'],
                                    fontName='Helvetica',fontSize=9,borderWidth=.7,
                                    borderColor=colors.HexColor('#94a3b8'),fillColor=colors.HexColor('#f8fafc'),
                                    textColor=colors.black,fieldFlags='required' if field['required'] else '',relative=True)
            else:
                c.acroForm.checkbox(name=field['name'],tooltip=field['label'],x=x,y=y,size=14,
                                    checked=False,buttonStyle='check',borderWidth=.7,
                                    borderColor=colors.HexColor('#94a3b8'),fillColor=colors.white,
                                    fieldFlags='required',relative=True)
                paragraph('I have read and agree to this contract.<br/>'
                          'I am authorised to bind this party.<br/>'
                          'I intend the name above to be my signature.',x+21,231,width=204)
        paragraph('<b>Complete and exchange the signed copy.</b> Each party signs the same version. '
                  'The contract takes effect when the second party signs. Save the completed PDF and '
                  'send it to the other party. Do not change agreed terms or party details after signing '
                  'without fresh agreement and signatures. An appropriate electronic-signature service '
                  'may also be used.',0,162)
        paragraph('These are ordinary fillable signature-name and intention fields, not a cryptographic '
                  'signature or identity-verification service. No acceptance is recorded in the rental '
                  'app by this download. Have the terms and party details reviewed before signing.',0,95)
        c.setFillColor(colors.HexColor('#64748b'));c.setFont('Helvetica',7)
        c.drawString(0,35,'Contract text SHA-256 (identifies the source terms, not the completed PDF):')
        c.setFont('Courier',7);c.drawString(0,23,self.digest)


def contract_pdf():
    from app.services.popia_documents import document_pdf
    return document_pdf(contract_text(),'Sano Trailers and Jackapp — Customer Information Contract',signing_page=True)
