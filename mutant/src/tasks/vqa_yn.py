# coding=utf-8
# Copyleft 2019 project LXRT.

import os
import collections

import torch
import torch.nn as nn
from torch.utils.data.dataloader import DataLoader
from tqdm import tqdm

from param import args
from pretrain.qa_answer_table import load_lxmert_qa
from tasks.vqa_model_yn import VQAModel
from tasks.vqa_data_yn import VQADataset, VQATorchDataset, VQAEvaluator

from email.mime.text import MIMEText
from subprocess import Popen, PIPE
import socket

DataTuple = collections.namedtuple("DataTuple", 'dataset loader evaluator')


def get_data_tuple(splits: str, bs:int, shuffle=False, drop_last=False, folder="/",nops=None) -> DataTuple:
    dset = VQADataset(splits,folder,nops=nops)
    tset = VQATorchDataset(dset)
    evaluator = VQAEvaluator(dset)
    data_loader = DataLoader(
        tset, batch_size=bs,
        shuffle=shuffle, num_workers=args.num_workers,
        drop_last=drop_last, pin_memory=True
    )

    return DataTuple(dataset=dset, loader=data_loader, evaluator=evaluator)


class VQA:
    def __init__(self,folder="/",load=True):
        # Datasets
        if load:
            self.train_tuple = get_data_tuple(
                args.train, bs=args.batch_size, shuffle=True, drop_last=True,folder=folder
            )
            if args.valid != "":
                self.valid_tuple = get_data_tuple(
                    args.valid, bs=128,
                    shuffle=False, drop_last=False, folder=folder
                )
            else:
                self.valid_tuple = None
        
        # Model
#         self.model = VQAModel(self.train_tuple.dataset.num_answers)
        self.model = VQAModel(3129,fn_type=args.fn_type)

        # Load pre-trained weights
        if args.load_lxmert is not None:
            self.model.lxrt_encoder.load(args.load_lxmert)
        if args.load_lxmert_qa is not None:
            load_lxmert_qa(args.load_lxmert_qa, self.model, label2ans=self.train_tuple.dataset.label2ans)
        
        # GPU options
        self.model = self.model.cuda()
        if args.multiGPU:
            self.model.lxrt_encoder.multi_gpu()

        # Loss and Optimizer
        self.bce_loss = nn.BCEWithLogitsLoss()
        if load :
            if 'bert' in args.optim:
                batch_per_epoch = len(self.train_tuple.loader)
                t_total = int(batch_per_epoch * args.epochs)
                print("BertAdam Total Iters: %d" % t_total)
                from lxrt.optimization import BertAdam
                self.optim = BertAdam(list(self.model.parameters()),
                                      lr=args.lr,
                                      warmup=0.1,
                                      t_total=t_total)
            else:
                self.optim = args.optimizer(self.model.parameters(), args.lr)
            # Output Directory
            self.output = args.output
            os.makedirs(self.output, exist_ok=True)

    def train(self, train_tuple, eval_tuple):
        dset, loader, evaluator = train_tuple
        iter_wrapper = (lambda x: tqdm(x, total=len(loader),ascii=True)) if args.tqdm else (lambda x: x)

        best_valid = 0.
        for epoch in range(args.epochs):
            quesid2ans = {}
            for i, (ques_id, feats, boxes, sent, yntypetarget, target) in iter_wrapper(enumerate(loader)):

                self.model.train()
                self.optim.zero_grad()

                feats, boxes, target, yntypetarget = feats.cuda(), boxes.cuda(), target.cuda(), yntypetarget.cuda()
                logit,yn_type_logit = self.model(feats, boxes, sent)
                assert logit.dim() == target.dim() == 2
                assert yn_type_logit.dim() == yntypetarget.dim() == 2
                
                loss = self.bce_loss(logit, target)
                loss = loss * logit.size(1)
                
                loss_yn = self.bce_loss(yn_type_logit,yntypetarget)
                loss_yn = loss_yn*yn_type_logit.size(1)
                
                loss = 0.9*loss + 0.1*loss_yn

                loss.backward()
                nn.utils.clip_grad_norm_(self.model.parameters(), 5.)
                self.optim.step()

                score, label = logit.max(1)
                for qid, l in zip(ques_id, label.cpu().numpy()):
                    ans = dset.label2ans[l]
                    quesid2ans[qid.item()] = ans

            log_str = "\nEpoch %d: Train %0.2f\n" % (epoch, evaluator.evaluate(quesid2ans) * 100.)

            if self.valid_tuple is not None:  # Do Validation
                valid_score = self.evaluate(eval_tuple)
                if valid_score > best_valid:
                    best_valid = valid_score
                    self.save("BEST")

                log_str += "Epoch %d: Valid %0.2f\n" % (epoch, valid_score * 100.) + \
                           "Epoch %d: Best %0.2f\n" % (epoch, best_valid * 100.)

            print(log_str, end='')

            with open(self.output + "/log.log", 'a') as f:
                f.write(log_str)
                f.flush()

        self.save("LAST")
        return best_valid

    def predict(self, eval_tuple: DataTuple, dump=None):
        """
        Predict the answers to questions in a data split.

        :param eval_tuple: The data tuple to be evaluated.
        :param dump: The path of saved file to dump results.
        :return: A dict of question_id to answer.
        """
        self.model.eval()
        dset, loader, evaluator = eval_tuple
        quesid2ans = {}
        
        type_accuracy = 0.0
        yn_type_accuracy = 0.0
        num_batches=0
        for i, datum_tuple in tqdm(enumerate(loader),ascii=True,desc="Evaluating"):
            ques_id, feats, boxes, sent, yesnotypetargets, target = datum_tuple   # Avoid seeing ground truth
            with torch.no_grad():
                feats, boxes = feats.cuda(), boxes.cuda()
                logit,yn_type_logit = self.model(feats, boxes, sent)
                score, label = logit.max(1)
                for qid, l in zip(ques_id, label.cpu().numpy()):
                    ans = dset.label2ans[l]
                    quesid2ans[qid.item()] = ans
                    
                yn_type_accuracy+= torch.mean(torch.eq((yn_type_logit>0.5).float().cuda(),yesnotypetargets.cuda()).float().cuda()).cpu().item()
                num_batches+=1
        
        print("YN Accuracy:",yn_type_accuracy/num_batches)
                
        if dump is not None:
            evaluator.dump_result(quesid2ans, dump)
        return quesid2ans

    def evaluate(self, eval_tuple: DataTuple, dump=None):
        """Evaluate all data in data_tuple."""
        quesid2ans = self.predict(eval_tuple, dump)
        return eval_tuple.evaluator.evaluate(quesid2ans)

    @staticmethod
    def oracle_score(data_tuple):
        dset, loader, evaluator = data_tuple
        quesid2ans = {}
        for i, datum_tuple in enumerate(loader):
            ques_id, feats, boxes, sent, yntypetarget, target = datum_tuple
            _, label = target.max(1)
            for qid, l in zip(ques_id, label.cpu().numpy()):
                ans = dset.label2ans[l]
                quesid2ans[qid.item()] = ans
        return evaluator.evaluate(quesid2ans)

    def save(self, name):
        model_to_save = self.model.module if hasattr(self.model, "module") else self.model
        torch.save(model_to_save.state_dict(),
                   os.path.join(self.output, "%s.pth" % name))

    def load(self, path):
        print("Load model from %s" % path)
        state_dict = torch.load("%s.pth" % path)
        self.model.load_state_dict(state_dict)


if __name__ == "__main__":


    # Test or Train
    if args.test is not None:
            # Build Class
        vqa = VQA(folder=args.data,load=False)
            # Load VQA model weights
        # Note: It is different from loading LXMERT pre-trained weights.
        if args.load is not None:
            vqa.load(args.load)
        args.fast = args.tiny = False       # Always loading all data in test
        if 'test' in args.test:
            vqa.predict(
                get_data_tuple(args.test, bs=950,
                               shuffle=False, drop_last=False,folder=args.data),
                dump=os.path.join(args.output, 'test_predict.json')
            )
        elif int(args.nops)>1 and 'val' in args.test and not args.dump:
            for data in ["varlenlol","varlencwl"]:
                for nops in range(2,6): 
                    print("Data folder:",data, "Nops:",nops)
                    args.data=data
                    result = vqa.evaluate(
                        get_data_tuple('minival', bs=950,
                                       shuffle=False, drop_last=False,folder=args.data,nops=nops),
                        dump=os.path.join(args.output, 'yn_val_predict.json')
                    )
                    print("Current Result:"+str(result))
                    print("\n\n\n\n\n")
                
        elif 'val' in args.test and not args.dump and args.nops==1:    
            # Since part of valididation data are used in pre-training/fine-tuning,
            # only validate on the minival set.
#             for data in ["orig","vqaorigyn","vqasinglelol","vqamultilol","vqalolval","vqacwlsingle","vqacwlmulti","vqacwlval","varlenlol","varlencwl","comma"]:
            for data in ["flipped_lol","ordered_lol","flipped_cwl","ordered_cwl"]:
                print("Data folder:",data)
                args.data=data
                result = vqa.evaluate(
                    get_data_tuple('minival', bs=950,
                                   shuffle=False, drop_last=False,folder=args.data,nops=args.nops),
                    dump=os.path.join(args.output, 'yn_val_predict.json')
                )
                print("Current Result:"+str(result))
                print("\n\n\n\n\n")        
        elif 'val' in args.test and args.dump:
            vqa.predict(
                    get_data_tuple("minival", bs=950,
                                   shuffle=False, drop_last=False,folder=args.data,nops=args.nops),
                    dump=os.path.join('data/vqa/'+args.data, 'yn_val_predict.json')
            )
        else:
            assert False, "No such test option for %s" % args.test
    else:
        # Build Class
        vqa = VQA(folder=args.data)
        # Load VQA model weights
        # Note: It is different from loading LXMERT pre-trained weights.
        if args.load is not None:
            vqa.load(args.load)
        
        print('Splits in Train data:', vqa.train_tuple.dataset.splits, flush=True)
        if vqa.valid_tuple is not None:
            print('Splits in Valid data:', vqa.valid_tuple.dataset.splits, flush=True)
            print("Valid Oracle: %0.2f" % (vqa.oracle_score(vqa.valid_tuple) * 100),flush=True)
        else:
            print("DO NOT USE VALIDATION")
        best_valid= vqa.train(vqa.train_tuple, vqa.valid_tuple)
        
        #Send Mail when training ends
        hostname = socket.gethostname()
        msg = MIMEText(str(args)+"\n HostName:"+str(hostname)+"\n")
        msg["From"] = "pbanerj6@asu.edu"
        msg["To"] = "pbanerj6@asu.edu"
        msg["Subject"] = "Job has ended. Valid Score:" + str(best_valid)
        p = Popen(["/usr/sbin/sendmail", "-t", "-oi"], stdin=PIPE)
        p.communicate(msg.as_bytes())

    
